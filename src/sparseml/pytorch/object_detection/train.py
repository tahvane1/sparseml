# YOLOv5 🚀 by Ultralytics, GPL-3.0 license

# Copyright (c) 2021 - present / Neuralmagic, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Train a YOLOv5 model on a custom dataset.
"""

import argparse
import math
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import torch
import torch.distributed as dist
import torch.nn as nn
import yaml
from torch.cuda import amp
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.optim import SGD, Adam, AdamW, lr_scheduler
from tqdm import tqdm

from sparseml.pytorch.object_detection import create_checkpoint, load_checkpoint, val
from yolov5.models.yolo import Model
from yolov5.utils.autoanchor import check_anchors
from yolov5.utils.autobatch import check_train_batch_size
from yolov5.utils.callbacks import Callbacks
from yolov5.utils.datasets import create_dataloader
from yolov5.utils.general import (
    LOGGER,
    ROOT,
    check_dataset,
    check_file,
    check_git_status,
    check_img_size,
    check_requirements,
    check_suffix,
    check_yaml,
    colorstr,
    get_latest_run,
    increment_path,
    init_seeds,
    labels_to_class_weights,
    labels_to_image_weights,
    methods,
    one_cycle,
    print_args,
    print_mutation,
    strip_optimizer,
)
from yolov5.utils.loggers import Loggers
from yolov5.utils.loggers.wandb.wandb_utils import check_wandb_resume
from yolov5.utils.loss import ComputeLoss
from yolov5.utils.metrics import fitness
from yolov5.utils.plots import plot_evolve, plot_labels
from yolov5.utils.sparse import SparseMLWrapper
from yolov5.utils.torch_utils import (
    EarlyStopping,
    ModelEMA,
    de_parallel,
    select_device,
    torch_distributed_zero_first,
)


FILE = Path(__file__).resolve()
LOCAL_ROOT = FILE.parents[0]
if str(LOCAL_ROOT) not in sys.path:
    sys.path.append(str(LOCAL_ROOT))  # add ROOT to PATH
LOCAL_ROOTOOT = Path(os.path.relpath(LOCAL_ROOT, Path.cwd()))  # relative


LOCAL_RANK = int(
    os.getenv("LOCAL_RANK", -1)
)  # https://pytorch.org/docs/stable/elastic/run.html
RANK = int(os.getenv("RANK", -1))
WORLD_SIZE = int(os.getenv("WORLD_SIZE", 1))


def train(hyp, opt, device, callbacks):  # path/to/hyp.yaml or hyp dictionary
    (
        save_dir,
        epochs,
        batch_size,
        weights,
        single_cls,
        evolve,
        data,
        cfg,
        resume,
        noval,
        nosave,  # noqa
        workers,
        freeze,
    ) = (
        Path(opt.save_dir),
        opt.epochs,
        opt.batch_size,
        opt.weights,
        opt.single_cls,
        opt.evolve,
        opt.data,
        opt.cfg,
        opt.resume,
        opt.noval,
        opt.nosave,
        opt.workers,
        opt.freeze,
    )

    # Directories
    w = save_dir / "weights"  # weights dir
    (w.parent if evolve else w).mkdir(parents=True, exist_ok=True)  # make dir
    last, best = w / "last.pt", w / "best.pt"

    # Hyperparameters
    if isinstance(hyp, str):
        with open(hyp, errors="ignore") as f:
            hyp = yaml.safe_load(f)  # load hyps dict
    LOGGER.info(
        colorstr("hyperparameters: ") + ", ".join(f"{k}={v}" for k, v in hyp.items())
    )

    # Save run settings
    if not evolve:
        with open(save_dir / "hyp.yaml", "w") as f:
            yaml.safe_dump(hyp, f, sort_keys=False)
        with open(save_dir / "opt.yaml", "w") as f:
            yaml.safe_dump(vars(opt), f, sort_keys=False)

    # Loggers
    data_dict = None
    if RANK in [-1, 0]:
        loggers = Loggers(save_dir, weights, opt, hyp, LOGGER)  # loggers instance
        if loggers.wandb:
            data_dict = loggers.wandb.data_dict
            if resume:
                weights, epochs, hyp, batch_size = (
                    opt.weights,
                    opt.epochs,
                    opt.hyp,
                    opt.batch_size,
                )

        # Register actions
        for k in methods(loggers):
            callbacks.register_action(k, callback=getattr(loggers, k))

    # Config
    plots = not evolve  # create plots
    cuda = device.type != "cpu"
    half_precision = cuda
    init_seeds(1 + RANK)
    with torch_distributed_zero_first(LOCAL_RANK):
        data_dict = data_dict or check_dataset(data)  # check if None
    train_path, val_path = data_dict["train"], data_dict["val"]
    nc = 1 if single_cls else int(data_dict["nc"])  # number of classes
    names = (
        ["item"] if single_cls and len(data_dict["names"]) != 1 else data_dict["names"]
    )  # class names
    assert (
        len(names) == nc
    ), f"{len(names)} names found for nc={nc} dataset in {data}"  # check
    is_coco = isinstance(val_path, str) and val_path.endswith(
        "coco/val2017.txt"
    )  # COCO dataset

    # Model
    check_suffix(weights, [".pt", ".pth"])  # check weights
    pretrained = (
        weights.endswith(".pt")
        or weights.endswith(".pth")
        or weights.startswith("zoo:")
    )
    if pretrained:
        model, extras = load_checkpoint(
            type_="train",
            weights=weights,
            device=device,
            cfg=opt.cfg,
            hyp=hyp,
            nc=nc,
            recipe=opt.recipe,
            resume=opt.resume,
            rank=LOCAL_RANK,
            one_shot=opt.one_shot,
        )
        ckpt, _, sparseml_wrapper = (
            extras["ckpt"],
            extras["state_dict"],
            extras["sparseml_wrapper"],
        )
        LOGGER.info(extras["report"])
    else:
        model = Model(cfg, ch=3, nc=nc, anchors=hyp.get("anchors")).to(device)  # create
        sparseml_wrapper = SparseMLWrapper(
            model,
            None,
            opt.recipe,
            steps_per_epoch=opt.max_train_steps,
            one_shot=opt.one_shot,
        )
        if not opt.one_shot:
            sparseml_wrapper.initialize(start_epoch=0)
        ckpt = None

    # Freeze
    freeze = [
        f"model.{x}." for x in (freeze if len(freeze) > 1 else range(freeze[0]))
    ]  # layers to freeze
    for k, v in model.named_parameters():
        v.requires_grad = True  # train all layers
        if any(x in k for x in freeze):
            LOGGER.info(f"freezing {k}")
            v.requires_grad = False

    # Image size
    gs = max(int(model.stride.max()), 32)  # grid size (max stride)
    imgsz = check_img_size(opt.imgsz, gs, floor=gs * 2)  # verify imgsz is gs-multiple

    # Batch size
    if RANK == -1 and batch_size == -1:  # single-GPU only, estimate best batch size
        batch_size = check_train_batch_size(model, imgsz)
        loggers.on_params_update({"batch_size": batch_size})

    # Optimizer
    nbs = 64  # nominal batch size
    accumulate = max(round(nbs / batch_size), 1)  # accumulate loss before optimizing
    hyp["weight_decay"] *= batch_size * accumulate / nbs  # scale weight_decay
    LOGGER.info(f"Scaled weight_decay = {hyp['weight_decay']}")

    g0, g1, g2 = [], [], []  # optimizer parameter groups
    for v in model.modules():
        if hasattr(v, "bias") and isinstance(v.bias, nn.Parameter):  # bias
            g2.append(v.bias)
        if isinstance(v, nn.BatchNorm2d):  # weight (no decay)
            g0.append(v.weight)
        elif hasattr(v, "weight") and isinstance(
            v.weight, nn.Parameter
        ):  # weight (with decay)
            g1.append(v.weight)

    if opt.optimizer == "Adam":
        optimizer = Adam(
            g0, lr=hyp["lr0"], betas=(hyp["momentum"], 0.999)
        )  # adjust beta1 to momentum
    elif opt.optimizer == "AdamW":
        optimizer = AdamW(
            g0, lr=hyp["lr0"], betas=(hyp["momentum"], 0.999)
        )  # adjust beta1 to momentum
    else:
        optimizer = SGD(g0, lr=hyp["lr0"], momentum=hyp["momentum"], nesterov=True)

    optimizer.add_param_group(
        {"params": g1, "weight_decay": hyp["weight_decay"]}
    )  # add g1 with weight_decay
    optimizer.add_param_group({"params": g2})  # add g2 (biases)
    LOGGER.info(
        f"{colorstr('optimizer:')} {type(optimizer).__name__} with parameter groups "
        f"{len(g0)} weight (no decay), {len(g1)} weight, {len(g2)} bias"
    )
    del g0, g1, g2

    # Scheduler
    if opt.cos_lr:
        lf = one_cycle(1, hyp["lrf"], epochs)  # cosine 1->hyp['lrf']
    else:
        lf = (
            lambda x: (1 - x / epochs) * (1.0 - hyp["lrf"]) + hyp["lrf"]
        )  # linear # noqa
    scheduler = lr_scheduler.LambdaLR(
        optimizer, lr_lambda=lf
    )  # plot_lr_scheduler(optimizer, scheduler, epochs)

    # EMA
    ema = ModelEMA(model, enabled=not opt.disable_ema) if RANK in [-1, 0] else None

    # Resume
    start_epoch = sparseml_wrapper.start_epoch or 0
    best_fitness = 0.0
    if pretrained:
        if opt.resume:
            assert (
                start_epoch > 0
            ), "%s training to %g epochs is finished, nothing to resume." % (
                weights,
                epochs,
            )
        if epochs < start_epoch:
            LOGGER.info(
                "%s has been trained for %g epochs. Fine-tuning for %g "
                "additional epochs." % (weights, start_epoch - 1, epochs)
            )
            epochs += start_epoch  # finetune additional epochs
        if sparseml_wrapper.qat_active(start_epoch):
            ema.enabled = False

        # Optimizer
        if ckpt["optimizer"] is not None:
            optimizer.load_state_dict(ckpt["optimizer"])
            best_fitness = ckpt["best_fitness"]

        # EMA
        if ema and ckpt.get("ema"):
            ema.ema.load_state_dict(ckpt["ema"].float().state_dict())
            ema.updates = ckpt["updates"]

        del ckpt

    # DP mode
    if cuda and RANK == -1 and torch.cuda.device_count() > 1:
        LOGGER.warning(
            "WARNING: DP not recommended, use torch.distributed.run for best "
            "DDP Multi-GPU results.\nSee Multi-GPU Tutorial at "
            "https://github.com/ultralytics/yolov5/issues/475 to get started."
        )
        model = torch.nn.DataParallel(model)

    # SyncBatchNorm
    if opt.sync_bn and cuda and RANK != -1:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model).to(device)
        LOGGER.info("Using SyncBatchNorm()")

    # Trainloader
    train_loader, dataset = create_dataloader(
        train_path,
        imgsz,
        batch_size // WORLD_SIZE,
        gs,
        single_cls,
        hyp=hyp,
        augment=True,
        cache=None if opt.cache == "val" else opt.cache,
        rect=opt.rect,
        rank=LOCAL_RANK,
        workers=workers,
        image_weights=opt.image_weights,
        quad=opt.quad,
        prefix=colorstr("train: "),
        shuffle=True,
    )
    mlc = int(np.concatenate(dataset.labels, 0)[:, 0].max())  # max label class
    nb = len(train_loader)  # number of batches
    assert mlc < nc, (
        f"Label class {mlc} exceeds nc={nc} in {data}. Possible class labels "
        "are 0-{nc - 1}"
    )

    # Process 0
    if RANK in [-1, 0]:
        val_loader = create_dataloader(
            val_path,
            imgsz,
            batch_size // WORLD_SIZE * 2,
            gs,
            single_cls,
            hyp=hyp,
            cache=None if noval else opt.cache,
            rect=True,
            rank=-1,
            workers=workers * 2,
            pad=0.5,
            prefix=colorstr("val: "),
        )[0]

        if not resume:
            labels = np.concatenate(dataset.labels, 0)
            # c = torch.tensor(labels[:, 0])  # classes
            # cf = torch.bincount(c.long(), minlength=nc) + 1.  # frequency
            # model._initialize_biases(cf.to(device))
            if plots:
                plot_labels(labels, names, save_dir)

            # Anchors
            if not opt.noautoanchor:
                check_anchors(dataset, model=model, thr=hyp["anchor_t"], imgsz=imgsz)

        callbacks.run("on_pretrain_routine_end")

    # DDP mode
    if cuda and RANK != -1:
        model = DDP(model, device_ids=[LOCAL_RANK], output_device=LOCAL_RANK)

    # Model attributes
    nl = de_parallel(model).model[-1].nl  # number of detection layers (to scale hyps)
    hyp["box"] *= 3 / nl  # scale to layers
    hyp["cls"] *= nc / 80 * 3 / nl  # scale to classes and layers
    hyp["obj"] *= (imgsz / 640) ** 2 * 3 / nl  # scale to image size and layers
    hyp["label_smoothing"] = opt.label_smoothing
    model.nc = nc  # attach number of classes to model
    model.hyp = hyp  # attach hyperparameters to model
    model.class_weights = (
        labels_to_class_weights(dataset.labels, nc).to(device) * nc
    )  # attach class weights
    model.names = names

    # Start training
    t0 = time.time()
    nw = max(
        round(hyp["warmup_epochs"] * nb), 100
    )  # number of warmup iterations, max(3 epochs, 100 iterations)
    # nw = min(nw, (epochs - start_epoch) / 2 * nb)  # limit warmup to < 1/2 of training
    last_opt_step = -1
    maps = np.zeros(nc)  # mAP per class
    results = (0, 0, 0, 0, 0, 0, 0)  # P, R, mAP@.5, mAP@.5-.95, val_loss(box, obj, cls)
    if scheduler:
        scheduler.last_epoch = start_epoch - 1  # do not mov
    scaler = amp.GradScaler(enabled=half_precision)
    stopper = EarlyStopping(patience=opt.patience)
    compute_loss = ComputeLoss(model)  # init loss class
    LOGGER.info(
        f"Image sizes {imgsz} train, {imgsz} val\n"
        f"Using {train_loader.num_workers * WORLD_SIZE} dataloader workers\n"
        f"Logging results to {colorstr('bold', save_dir)}\n"
        f"Starting training for {epochs} epochs..."
    )

    # SparseML Integration
    if opt.one_shot:
        # skip training and save model on one-shot
        LOGGER.info(f"Skipped training due to one-shot: {opt.one_shot}")

        # create and save checkpoint
        ckpt_extras = {
            "nc": nc,
            "best_fitness": best_fitness,
            "wandb_id": loggers.wandb.wandb_run.id if loggers.wandb else None,
            "date": datetime.now().isoformat(),
        }
        ckpt = create_checkpoint(
            -1, model, optimizer, ema, sparseml_wrapper, **ckpt_extras
        )
        one_shot_checkpoint_name = w / "checkpoint-one-shot.pt"
        torch.save(ckpt, one_shot_checkpoint_name)
        LOGGER.info(f"One shot checkpoint saved to {one_shot_checkpoint_name}")

        if opt.num_export_samples > 0:
            dataloader = val_loader or train_loader
            sparseml_wrapper.save_sample_inputs_outputs(
                dataloader=dataloader,
                num_export_samples=opt.num_export_samples,
                save_dir=str(w),
            )

        torch.cuda.empty_cache()
        return results

    # Continue as expected
    if RANK in [-1, 0]:
        sparseml_wrapper.initialize_loggers(loggers.logger, loggers.tb, loggers.wandb)
    scaler = sparseml_wrapper.modify(scaler, optimizer, model, train_loader)
    scheduler = sparseml_wrapper.check_lr_override(scheduler, RANK)
    epochs = sparseml_wrapper.check_epoch_override(epochs, RANK)
    train_steps_executed = 0
    for epoch in range(
        start_epoch, epochs
    ):  # epoch ------------------------------------------------------------------
        if sparseml_wrapper.qat_active(epoch):
            LOGGER.info("Disabling half precision and EMA, QAT scheduled to run")
            half_precision = False
            scaler._enabled = False
            ema.enabled = False
        model.train()

        # Update image weights (optional, single-GPU only)
        if opt.image_weights:
            cw = (
                model.class_weights.cpu().numpy() * (1 - maps) ** 2 / nc
            )  # class weights
            iw = labels_to_image_weights(
                dataset.labels, nc=nc, class_weights=cw
            )  # image weights
            dataset.indices = random.choices(
                range(dataset.n), weights=iw, k=dataset.n
            )  # rand weighted idx

        # Update mosaic border (optional)
        # b = int(random.uniform(0.25 * imgsz, 0.75 * imgsz + gs) // gs * gs)
        # dataset.mosaic_border = [b - imgsz, -b]  # height, width borders

        mloss = torch.zeros(3, device=device)  # mean losses
        if RANK != -1:
            train_loader.sampler.set_epoch(epoch)
        pbar = enumerate(train_loader)
        LOGGER.info(
            ("\n" + "%10s" * 7)
            % ("Epoch", "gpu_mem", "box", "obj", "cls", "labels", "img_size")
        )
        if RANK in [-1, 0]:
            steps = opt.max_train_steps if opt.max_train_steps > 0 else nb
            pbar = tqdm(
                pbar, total=steps, bar_format="{l_bar}{bar:10}{r_bar}{bar:-10b}"
            )  # progress bar
        optimizer.zero_grad()
        for i, (
            imgs,
            targets,
            paths,
            _,
        ) in (
            pbar
        ):  # batch -------------------------------------------------------------
            ni = i + nb * epoch  # number integrated batches (since train start)
            imgs = (
                imgs.to(device, non_blocking=True).float() / 255
            )  # uint8 to float32, 0-255 to 0.0-1.0

            # Warmup
            if ni <= nw:
                xi = [0, nw]  # x interp
                # compute_loss.gr = np.interp(ni, xi, [0.0, 1.0])  # iou loss ratio
                # (obj_loss = 1.0 or iou)
                accumulate = max(1, np.interp(ni, xi, [1, nbs / batch_size]).round())
                for j, x in enumerate(optimizer.param_groups):
                    # bias lr falls from 0.1 to lr0, all other lrs rise from 0.0 to lr0
                    if scheduler:
                        x["lr"] = np.interp(
                            ni,
                            xi,
                            [
                                hyp["warmup_bias_lr"] if j == 2 else 0.0,
                                x["initial_lr"] * lf(epoch),
                            ],
                        )
                    if "momentum" in x:
                        x["momentum"] = np.interp(
                            ni, xi, [hyp["warmup_momentum"], hyp["momentum"]]
                        )

            # Multi-scale
            if opt.multi_scale:
                sz = random.randrange(imgsz * 0.5, imgsz * 1.5 + gs) // gs * gs  # size
                sf = sz / max(imgs.shape[2:])  # scale factor
                if sf != 1:
                    ns = [
                        math.ceil(x * sf / gs) * gs for x in imgs.shape[2:]
                    ]  # new shape (stretched to gs-multiple)
                    imgs = nn.functional.interpolate(
                        imgs, size=ns, mode="bilinear", align_corners=False
                    )

            # Forward
            with amp.autocast(enabled=half_precision):
                pred = model(imgs)  # forward
                loss, loss_items = compute_loss(
                    pred, targets.to(device)
                )  # loss scaled by batch_size
                if RANK != -1:
                    loss *= WORLD_SIZE  # gradient averaged between devices in DDP mode
                if opt.quad:
                    loss *= 4.0

            # Backward
            scaler.scale(loss).backward()
            train_steps_executed += 1

            # Optimize
            if ni - last_opt_step >= accumulate:
                scaler.step(optimizer)  # optimizer.step
                scaler.update()
                optimizer.zero_grad()
                if ema:
                    ema.update(model)
                last_opt_step = ni
            elif hasattr(scaler, "emulated_step"):
                # Call for SparseML integration since the number of steps per epoch
                # can vary. This keeps the number of steps per epoch equivalent to the
                # number of batches per epoch. Does not step the scaler or the optimizer
                scaler.emulated_step()

            # Log
            if RANK in [-1, 0]:
                mloss = (mloss * i + loss_items) / (i + 1)  # update mean losses
                mem = "{:.3f}G".format(
                    torch.cuda.memory_reserved() / 1e9
                    if torch.cuda.is_available()
                    else 0
                )  # (GB)
                pbar.set_description(
                    ("%10s" * 2 + "%10.4g" * 5)
                    % (
                        f"{epoch}/{epochs - 1}",
                        mem,
                        *mloss,
                        targets.shape[0],
                        imgs.shape[-1],
                    )
                )
                callbacks.run(
                    "on_train_batch_end",
                    ni,
                    model,
                    imgs,
                    targets,
                    paths,
                    plots,
                    opt.sync_bn,
                )
                if callbacks.stop_training:
                    return

            if 0 < opt.max_train_steps <= train_steps_executed:
                LOGGER.info(
                    f"\nStopping early, {train_steps_executed} training steps executed"
                )
                break
            # end batch ---------------------------------------------------------------

        # Scheduler
        lr = [x["lr"] for x in optimizer.param_groups]  # for loggers
        if scheduler:
            scheduler.step()

        if RANK in [-1, 0]:
            # mAP
            callbacks.run("on_train_epoch_end", epoch=epoch)
            ema.update_attr(
                model, include=["yaml", "nc", "hyp", "names", "stride", "class_weights"]
            )
            final_epoch = (epoch + 1 == epochs) or stopper.possible_stop
            if not noval or final_epoch:  # Calculate mAP
                results, maps, _ = val(
                    data_dict,
                    batch_size=batch_size // WORLD_SIZE * 2,
                    imgsz=imgsz,
                    model=ema.ema,
                    single_cls=single_cls,
                    dataloader=val_loader,
                    save_dir=save_dir,
                    plots=False,
                    callbacks=callbacks,
                    compute_loss=compute_loss,
                    half=half_precision,
                    max_steps=opt.max_eval_steps,
                )

            # Update best mAP
            fi = fitness(
                np.array(results).reshape(1, -1)
            )  # weighted combination of [P, R, mAP@.5, mAP@.5-.95]
            if fi > best_fitness or sparseml_wrapper.reset_best(epoch):
                best_fitness = fi
            log_vals = list(mloss) + list(results) + lr
            callbacks.run("on_fit_epoch_end", log_vals, epoch, best_fitness, fi)

            # Save model
            if (not opt.nosave) or (final_epoch and not opt.evolve):  # if save
                ckpt_extras = {
                    "nc": nc,
                    "best_fitness": best_fitness,
                    "wandb_id": loggers.wandb.wandb_run.id if loggers.wandb else None,
                    "date": datetime.now().isoformat(),
                }
                ckpt = create_checkpoint(
                    epoch, model, optimizer, ema, sparseml_wrapper, **ckpt_extras
                )

                # Save last, best and delete
                torch.save(ckpt, last)
                if best_fitness == fi:
                    torch.save(ckpt, best)
                if (
                    (epoch > 0)
                    and (opt.save_period > 0)
                    and (epoch % opt.save_period == 0)
                ):
                    torch.save(ckpt, w / f"epoch{epoch}.pt")
                del ckpt
                callbacks.run(
                    "on_model_save", last, epoch, final_epoch, best_fitness, fi
                )

            # Stop Single-GPU
            if RANK == -1 and stopper(epoch=epoch, fitness=fi):
                break

            # Stop DDP TODO: known issues
            # shttps://github.com/ultralytics/yolov5/pull/4576
            # stop = stopper(epoch=epoch, fitness=fi)
            # if RANK == 0:
            #    dist.broadcast_object_list([stop], 0)  # broadcast 'stop' to all ranks

        # Stop DPP
        # with torch_distributed_zero_first(RANK):
        # if stop:
        #    break  # must break all DDP ranks
        if 0 < opt.max_train_steps <= train_steps_executed:
            break
        # end epoch -------------------------------------------------------------------
    # end training --------------------------------------------------------------------
    if RANK in [-1, 0]:
        epochs_ = epochs - start_epoch + 1
        if opt.max_train_steps > 0:
            epochs_ = opt.max_train_steps / nb
        LOGGER.info(
            f"\n{epochs_} epochs completed in "
            f"{(time.time() - t0) / 3600:.3f} hours."
        )
        for f in last, best:
            if f.exists():
                strip_optimizer(f)  # strip optimizers
                if f is best:
                    LOGGER.info(f"\nValidating {f}...")
                    results, _, _ = val(
                        data_dict,
                        batch_size=batch_size // WORLD_SIZE * 2,
                        imgsz=imgsz,
                        model=load_checkpoint(
                            type_="ensemble", weights=best, device=device
                        )[0],
                        iou_thres=0.65
                        if is_coco
                        else 0.60,  # best pycocotools results at 0.65
                        single_cls=single_cls,
                        dataloader=val_loader,
                        save_dir=save_dir,
                        save_json=is_coco,
                        verbose=True,
                        plots=True,
                        callbacks=callbacks,
                        compute_loss=compute_loss,  # val best model with plots
                        half=half_precision,
                    )
                    if is_coco:
                        callbacks.run(
                            "on_fit_epoch_end",
                            list(mloss) + list(results) + lr,
                            epoch,
                            best_fitness,
                            fi,
                        )

        callbacks.run("on_train_end", last, best, plots, epoch, results)
        LOGGER.info(f"Results saved to {colorstr('bold', save_dir)}")

    if opt.num_export_samples > 0:
        sparseml_wrapper.save_sample_inputs_outputs(
            dataloader=val_loader or train_loader,
            num_export_samples=opt.num_export_samples,
            save_dir=str(w),
        )

    torch.cuda.empty_cache()
    return results


def parse_opt(known=False):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--weights", type=str, default=ROOT / "yolov5s.pt", help="initial weights path"
    )
    parser.add_argument("--cfg", type=str, default="", help="model.yaml path")
    parser.add_argument(
        "--data", type=str, default=ROOT / "data/coco128.yaml", help="dataset.yaml path"
    )
    parser.add_argument(
        "--hyp",
        type=str,
        default=ROOT / "data/hyps/hyp.scratch-low.yaml",
        help="hyperparameters path",
    )
    parser.add_argument("--epochs", type=int, default=300)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=16,
        help="total batch size for all GPUs, -1 for autobatch",
    )
    parser.add_argument(
        "--imgsz",
        "--img",
        "--img-size",
        type=int,
        default=640,
        help="train, val image size (pixels)",
    )
    parser.add_argument("--rect", action="store_true", help="rectangular training")
    parser.add_argument(
        "--resume",
        nargs="?",
        const=True,
        default=False,
        help=(
            "resume most recent training. When true, ignores --recipe arg and "
            "re-uses saved recipe (if exists)"
        ),
    )
    parser.add_argument(
        "--nosave", action="store_true", help="only save final checkpoint"
    )
    parser.add_argument(
        "--noval", action="store_true", help="only validate final epoch"
    )
    parser.add_argument(
        "--noautoanchor", action="store_true", help="disable AutoAnchor"
    )
    parser.add_argument(
        "--evolve",
        type=int,
        nargs="?",
        const=300,
        help="evolve hyperparameters for x generations",
    )
    parser.add_argument("--bucket", type=str, default="", help="gsutil bucket")
    parser.add_argument(
        "--cache",
        type=str,
        nargs="?",
        const="ram",
        help='--cache images in "ram" (default) or "disk"',
    )
    parser.add_argument(
        "--image-weights",
        action="store_true",
        help="use weighted image selection for training",
    )
    parser.add_argument(
        "--device", default="", help="cuda device, i.e. 0 or 0,1,2,3 or cpu"
    )
    parser.add_argument(
        "--multi-scale", action="store_true", help="vary img-size +/- 50%%"
    )
    parser.add_argument(
        "--single-cls",
        action="store_true",
        help="train multi-class data as single-class",
    )
    parser.add_argument(
        "--optimizer",
        type=str,
        choices=["SGD", "Adam", "AdamW"],
        default="SGD",
        help="optimizer",
    )
    parser.add_argument(
        "--sync-bn",
        action="store_true",
        help="use SyncBatchNorm, only available in DDP mode",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="max dataloader workers (per RANK in DDP mode)",
    )
    parser.add_argument(
        "--project", default=LOCAL_ROOT / "runs/train", help="save to project/name"
    )
    parser.add_argument("--name", default="exp", help="save to project/name")
    parser.add_argument(
        "--exist-ok",
        action="store_true",
        help="existing project/name ok, do not increment",
    )
    parser.add_argument("--quad", action="store_true", help="quad dataloader")
    parser.add_argument("--cos-lr", action="store_true", help="cosine LR scheduler")
    parser.add_argument(
        "--label-smoothing", type=float, default=0.0, help="Label smoothing epsilon"
    )
    parser.add_argument(
        "--patience",
        type=int,
        default=100,
        help="EarlyStopping patience (epochs without improvement)",
    )
    parser.add_argument(
        "--freeze",
        nargs="+",
        type=int,
        default=[0],
        help="Freeze layers: backbone=10, first3=0 1 2",
    )
    parser.add_argument(
        "--save-period",
        type=int,
        default=-1,
        help="Save checkpoint every x epochs (disabled if < 1)",
    )
    parser.add_argument(
        "--local_rank", type=int, default=-1, help="DDP parameter, do not modify"
    )

    # Weights & Biases arguments
    parser.add_argument("--entity", default=None, help="W&B: Entity")
    parser.add_argument(
        "--upload_dataset",
        nargs="?",
        const=True,
        default=False,
        help='W&B: Upload data, "val" option',
    )
    parser.add_argument(
        "--bbox_interval",
        type=int,
        default=-1,
        help="W&B: Set bounding-box image logging interval",
    )
    parser.add_argument(
        "--artifact_alias",
        type=str,
        default="latest",
        help="W&B: Version of dataset artifact to use",
    )
    parser.add_argument(
        "--recipe",
        type=str,
        default=None,
        help="Path to a sparsification recipe, "
        "see https://github.com/neuralmagic/sparseml for more information",
    )
    parser.add_argument(
        "--disable-ema",
        action="store_true",
        help="Disable EMA model updates (enabled by default)",
    )
    parser.add_argument(
        "--max-train-steps",
        type=int,
        default=-1,
        help="Set the maximum number of training steps per epoch. if negative,"
        "the entire dataset will be used, default=-1",
    )
    parser.add_argument(
        "--max-eval-steps",
        type=int,
        default=-1,
        help="Set the maximum number of eval steps per epoch. if negative,"
        "the entire dataset will be used, default=-1",
    )
    parser.add_argument(
        "--one-shot",
        action="store_true",
        default=False,
        help="Apply recipe in one shot manner",
    )
    parser.add_argument(
        "--num-export-samples",
        type=int,
        default=0,
        help="The number of sample inputs/outputs to export, default=0",
    )

    opt = parser.parse_known_args()[0] if known else parser.parse_args()
    return opt


def main(opt=None, callbacks=Callbacks()):
    if opt is None:
        opt = parse_opt()
    # Checks
    if RANK in [-1, 0]:
        print_args(FILE.stem, opt)
        check_git_status()
        check_requirements(exclude=["thop"])

    # Resume
    if (
        opt.resume and not check_wandb_resume(opt) and not opt.evolve
    ):  # resume an interrupted run
        ckpt = (
            opt.resume if isinstance(opt.resume, str) else get_latest_run()
        )  # specified or most recent path
        assert os.path.isfile(ckpt), "ERROR: --resume checkpoint does not exist"
        with open(Path(ckpt).parent.parent / "opt.yaml", errors="ignore") as f:
            opt = argparse.Namespace(**yaml.safe_load(f))  # replace
        opt.cfg, opt.weights, opt.resume = "", ckpt, True  # reinstate
        LOGGER.info(f"Resuming training from {ckpt}")
    else:
        opt.data, opt.cfg, opt.hyp, opt.weights, opt.project = (
            check_file(opt.data),
            check_yaml(opt.cfg),
            check_yaml(opt.hyp),
            str(opt.weights),
            str(opt.project),
        )  # checks
        assert len(opt.cfg) or len(
            opt.weights
        ), "either --cfg or --weights must be specified"
        if opt.evolve:
            if opt.project == str(
                LOCAL_ROOT / "runs/train"
            ):  # if default project name, rename to runs/evolve
                opt.project = str(LOCAL_ROOT / "runs/evolve")
            opt.exist_ok, opt.resume = (
                opt.resume,
                False,
            )  # pass resume to exist_ok and disable resume
        opt.save_dir = str(
            increment_path(Path(opt.project) / opt.name, exist_ok=opt.exist_ok)
        )  # increment run

    # DDP mode
    device = select_device(opt.device, batch_size=opt.batch_size)
    if LOCAL_RANK != -1:
        msg = "is not compatible with YOLOv5 Multi-GPU DDP training"
        assert not opt.image_weights, f"--image-weights {msg}"
        assert not opt.evolve, f"--evolve {msg}"
        assert (
            opt.batch_size != -1
        ), f"AutoBatch with --batch-size -1 {msg}, please pass a valid --batch-size"
        assert (
            opt.batch_size % WORLD_SIZE == 0
        ), f"--batch-size {opt.batch_size} must be multiple of WORLD_SIZE"
        assert (
            torch.cuda.device_count() > LOCAL_RANK
        ), "insufficient CUDA devices for DDP command"
        torch.cuda.set_device(LOCAL_RANK)
        device = torch.device("cuda", LOCAL_RANK)
        dist.init_process_group(backend="nccl" if dist.is_nccl_available() else "gloo")

    # Train
    if not opt.evolve:
        train(opt.hyp, opt, device, callbacks)
        if WORLD_SIZE > 1 and RANK == 0:
            LOGGER.info("Destroying process group... ")
            dist.destroy_process_group()

    # Evolve hyperparameters (optional)
    else:
        # Hyperparameter evolution metadata (mutation scale 0-1,
        # lower_limit, upper_limit)
        meta = {
            "lr0": (1, 1e-5, 1e-1),  # initial learning rate (SGD=1E-2, Adam=1E-3)
            "lrf": (1, 0.01, 1.0),  # final OneCycleLR learning rate (lr0 * lrf)
            "momentum": (0.3, 0.6, 0.98),  # SGD momentum/Adam beta1
            "weight_decay": (1, 0.0, 0.001),  # optimizer weight decay
            "warmup_epochs": (1, 0.0, 5.0),  # warmup epochs (fractions ok)
            "warmup_momentum": (1, 0.0, 0.95),  # warmup initial momentum
            "warmup_bias_lr": (1, 0.0, 0.2),  # warmup initial bias lr
            "box": (1, 0.02, 0.2),  # box loss gain
            "cls": (1, 0.2, 4.0),  # cls loss gain
            "cls_pw": (1, 0.5, 2.0),  # cls BCELoss positive_weight
            "obj": (1, 0.2, 4.0),  # obj loss gain (scale with pixels)
            "obj_pw": (1, 0.5, 2.0),  # obj BCELoss positive_weight
            "iou_t": (0, 0.1, 0.7),  # IoU training threshold
            "anchor_t": (1, 2.0, 8.0),  # anchor-multiple threshold
            "anchors": (2, 2.0, 10.0),  # anchors per output grid (0 to ignore)
            "fl_gamma": (
                0,
                0.0,
                2.0,
            ),  # focal loss gamma (efficientDet default gamma=1.5)
            "hsv_h": (1, 0.0, 0.1),  # image HSV-Hue augmentation (fraction)
            "hsv_s": (1, 0.0, 0.9),  # image HSV-Saturation augmentation (fraction)
            "hsv_v": (1, 0.0, 0.9),  # image HSV-Value augmentation (fraction)
            "degrees": (1, 0.0, 45.0),  # image rotation (+/- deg)
            "translate": (1, 0.0, 0.9),  # image translation (+/- fraction)
            "scale": (1, 0.0, 0.9),  # image scale (+/- gain)
            "shear": (1, 0.0, 10.0),  # image shear (+/- deg)
            "perspective": (
                0,
                0.0,
                0.001,
            ),  # image perspective (+/- fraction), range 0-0.001
            "flipud": (1, 0.0, 1.0),  # image flip up-down (probability)
            "fliplr": (0, 0.0, 1.0),  # image flip left-right (probability)
            "mosaic": (1, 0.0, 1.0),  # image mixup (probability)
            "mixup": (1, 0.0, 1.0),  # image mixup (probability)
            "copy_paste": (1, 0.0, 1.0),
        }  # segment copy-paste (probability)

        with open(opt.hyp, errors="ignore") as f:
            hyp = yaml.load(f, Loader=yaml.SafeLoader)  # load hyps dict
            if "anchors" not in hyp:  # anchors commented in hyp.yaml
                hyp["anchors"] = 3
        opt.noval, opt.nosave, save_dir = (
            True,
            True,
            Path(opt.save_dir),
        )  # only val/save final epoch
        # ei = [isinstance(x, (int, float)) for x in hyp.values()]  # evolvable indices
        evolve_yaml, evolve_csv = save_dir / "hyp_evolve.yaml", save_dir / "evolve.csv"
        if opt.bucket:
            os.system(
                f"gsutil cp gs://{opt.bucket}/evolve.csv {evolve_csv}"
            )  # download evolve.csv if exists

        for _ in range(opt.evolve):  # generations to evolve
            if evolve_csv.exists():  # if evolve.csv exists: select best hyps and mutate
                # Select parent(s)
                parent = "single"  # parent selection method: 'single' or 'weighted'
                x = np.loadtxt(evolve_csv, ndmin=2, delimiter=",", skiprows=1)
                n = min(5, len(x))  # number of previous results to consider
                x = x[np.argsort(-fitness(x))][:n]  # top n mutations
                w = fitness(x) - fitness(x).min() + 1e-6  # weights (sum > 0)
                if parent == "single" or len(x) == 1:
                    # x = x[random.randint(0, n - 1)]  # random selection
                    x = x[random.choices(range(n), weights=w)[0]]  # weighted selection
                elif parent == "weighted":
                    x = (x * w.reshape(n, 1)).sum(0) / w.sum()  # weighted combination

                # Mutate
                mp, s = 0.8, 0.2  # mutation probability, sigma
                npr = np.random
                npr.seed(int(time.time()))
                g = np.array([meta[k][0] for k in hyp.keys()])  # gains 0-1
                ng = len(meta)
                v = np.ones(ng)
                while all(v == 1):  # mutate until a change occurs (prevent duplicates)
                    v = (
                        g * (npr.random(ng) < mp) * npr.randn(ng) * npr.random() * s + 1
                    ).clip(0.3, 3.0)
                for i, k in enumerate(hyp.keys()):  # plt.hist(v.ravel(), 300)
                    hyp[k] = float(x[i + 7] * v[i])  # mutate

            # Constrain to limits
            for k, v in meta.items():
                hyp[k] = max(hyp[k], v[1])  # lower limit
                hyp[k] = min(hyp[k], v[2])  # upper limit
                hyp[k] = round(hyp[k], 5)  # significant digits

            # Train mutation
            results = train(hyp.copy(), opt, device, callbacks)
            callbacks = Callbacks()
            # Write mutation results
            print_mutation(results, hyp.copy(), save_dir, opt.bucket)

        # Plot results
        plot_evolve(evolve_csv)
        LOGGER.info(
            f"Hyperparameter evolution finished {opt.evolve} generations\n"
            f"Results saved to {colorstr('bold', save_dir)}\n"
            f"Usage example: $ python train.py --hyp {evolve_yaml}"
        )


def run(**kwargs):
    # Usage: import train; train.run(data='coco128.yaml', imgsz=320,
    # weights='yolov5m.pt')
    opt = parse_opt(True)
    for k, v in kwargs.items():
        setattr(opt, k, v)
    main(opt)
    return opt


if __name__ == "__main__":
    opt = parse_opt()
    main(opt)
