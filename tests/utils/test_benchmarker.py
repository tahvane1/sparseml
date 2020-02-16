import pytest

from collections import OrderedDict
import sys
import numpy
import torch
from torch.nn import Sequential, Linear

from neuralmagicML.utils import BatchBenchmarkResults, ModuleBenchmarker


@pytest.mark.device_cpu
def test_results_const():
    batch_size = 1
    results = BatchBenchmarkResults(batch_size)
    assert results.batch_size == batch_size
    assert len(results.model_batch_timings) < 1
    assert len(results.e2e_batch_timings) < 1


@pytest.mark.device_cpu
def test_results_add():
    batch_size = 1
    results = BatchBenchmarkResults(batch_size)

    results.add(1.0, 1.0, 1)
    assert len(results.model_batch_timings) == 1
    assert len(results.e2e_batch_timings) == 1

    with pytest.raises(ValueError):
        results.add(1.0, 1.0, 8)


@pytest.mark.parametrize("batch_size", [1, 64])
@pytest.mark.parametrize(
    "timings,avg",
    [([0.1, 0.1, 0.1, 0.1], 0.1), ([0.0, 1.0, 2.0], 1.0), ([0.0, -1.0, -2.0], -1.0)],
)
@pytest.mark.device_cpu
def test_results_props_single_batch_size(batch_size, timings, avg):
    results = BatchBenchmarkResults(batch_size)
    for val in timings:
        results.add(val, 0.0, batch_size)
    assert len(results.model_batch_timings) == len(timings)
    assert (
        abs(results.model_batch_seconds - numpy.average(timings))
        < sys.float_info.epsilon
    )
    assert (
        abs(results.model_batches_per_second - 1.0 / numpy.average(timings))
        < sys.float_info.epsilon
    )
    assert (
        abs(results.model_item_seconds - numpy.average(timings) / batch_size)
        < sys.float_info.epsilon
    )
    assert (
        abs(results.model_items_per_second - 1.0 / numpy.average(timings) * batch_size)
        < sys.float_info.epsilon
    )
    assert results.e2e_batch_seconds == 0.0
    assert results.e2e_batch_seconds == 0.0

    results = BatchBenchmarkResults(batch_size)
    for val in timings:
        results.add(0.0, val, batch_size)
    assert len(results.e2e_batch_timings) == len(timings)
    assert (
        abs(results.e2e_batch_seconds - numpy.average(timings)) < sys.float_info.epsilon
    )
    assert (
        abs(results.e2e_batches_per_second - 1.0 / numpy.average(timings))
        < sys.float_info.epsilon
    )
    assert (
        abs(results.e2e_item_seconds - numpy.average(timings) / batch_size)
        < sys.float_info.epsilon
    )
    assert (
        abs(results.e2e_items_per_second - 1.0 / numpy.average(timings) * batch_size)
        < sys.float_info.epsilon
    )
    assert results.model_batch_seconds == 0.0
    assert results.model_batch_seconds == 0.0


BENCHMARK_MODEL = Sequential(
    OrderedDict(
        [
            ("fc1", Linear(8, 16, bias=True)),
            ("fc2", Linear(16, 32, bias=True)),
            (
                "block1",
                Sequential(
                    OrderedDict(
                        [
                            ("fc1", Linear(32, 16, bias=True)),
                            ("fc2", Linear(16, 8, bias=True)),
                        ]
                    )
                ),
            ),
        ]
    )
)


def _results_sanity_check(
    results: BatchBenchmarkResults, test_size: int, batch_size: int
):
    assert len(results.model_batch_timings) == test_size
    assert len(results.e2e_batch_timings) == test_size
    assert results.batch_size == batch_size

    assert results.model_batch_seconds > 0.0
    assert results.model_batches_per_second > 0.0
    assert results.model_item_seconds > 0.0
    assert results.model_items_per_second > 0.0
    assert results.e2e_batch_seconds > 0.0
    assert results.e2e_batches_per_second > 0.0
    assert results.e2e_item_seconds > 0.0
    assert results.e2e_items_per_second > 0.0


@pytest.mark.parametrize("batch_size", [1, 64])
@pytest.mark.device_cpu
def test_benchmark_cpu(batch_size):
    benchmarker = ModuleBenchmarker(BENCHMARK_MODEL)
    batches = [torch.rand(batch_size, 8) for _ in range(10)]
    warmup_size = 5
    test_size = 30

    results = benchmarker.run_batches_on_device(
        batches,
        "cpu",
        full_precision=True,
        warmup_size=warmup_size,
        test_size=test_size,
    )
    _results_sanity_check(results, test_size, batch_size)


@pytest.mark.parametrize("batch_size", [1, 64])
@pytest.mark.device_cuda
def test_benchmark_cuda_full(batch_size):
    benchmarker = ModuleBenchmarker(BENCHMARK_MODEL)
    batches = [torch.rand(batch_size, 8) for _ in range(10)]
    warmup_size = 5
    test_size = 30

    results = benchmarker.run_batches_on_device(
        batches,
        "cuda",
        full_precision=True,
        warmup_size=warmup_size,
        test_size=test_size,
    )
    _results_sanity_check(results, test_size, batch_size)


@pytest.mark.parametrize("batch_size", [1, 64])
@pytest.mark.device_cuda
def test_benchmark_cuda_full(batch_size):
    benchmarker = ModuleBenchmarker(BENCHMARK_MODEL)
    batches = [torch.rand(batch_size, 8) for _ in range(10)]
    warmup_size = 5
    test_size = 30

    results = benchmarker.run_batches_on_device(
        batches,
        "cuda",
        full_precision=False,
        warmup_size=warmup_size,
        test_size=test_size,
    )
    _results_sanity_check(results, test_size, batch_size)
