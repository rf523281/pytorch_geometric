import torch

from torch_geometric.nn import (
    MeanAggregation,
    SumAggregation,
    VariancePreservingAggregation,
)


def test_variance_preserving():
    x = torch.randn(6, 16)
    index = torch.tensor([0, 0, 1, 1, 1, 3])
    ptr = torch.tensor([0, 2, 5, 5, 6])

    vpa_aggr = VariancePreservingAggregation()
    mean_aggr = MeanAggregation()
    sum_aggr = SumAggregation()

    out_vpa = vpa_aggr(x, index)
    out_mean = mean_aggr(x, index)
    out_sum = sum_aggr(x, index)

    # Equivalent formulation:
    expected = torch.sqrt(out_mean.abs() * out_sum.abs()) * out_sum.sign()

    assert out_vpa.size() == (4, 16)
    assert torch.allclose(out_vpa, expected)
    assert torch.allclose(out_vpa, vpa_aggr(x, ptr=ptr))
