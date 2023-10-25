import argparse
import os
import time
from typing import Optional

import torch
import torch.nn.functional as F
from ogb.nodeproppred import PygNodePropPredDataset

import torch_geometric

parser = argparse.ArgumentParser()
parser.add_argument('--hidden_channels', type=int, default=128)
parser.add_argument('--num_layers', type=int, default=2)
parser.add_argument('--lr', type=float, default=0.001)
parser.add_argument('--epochs', type=int, default=3)
parser.add_argument('--batch_size', type=int, default=2048)
parser.add_argument('--fan_out', type=int, default=16)
parser.add_argument(
    "--use_gat_conv",
    action='store_true',
    help="Wether or not to use GATConv. (Defaults to using GCNConv)",
)
parser.add_argument(
    "--n_gat_conv_heads",
    type=int,
    default=4,
    help="If using GATConv, number of attention heads to use",
)
parser.add_argument(
    "--cugraph_data_loader",
    action='store_true',
    help="Wether or not to use CuGraph for Neighbor Loading. \
        \nNote that this requires more GPU memory or \
        a reduction in batch_size/fan_out/hidden_channels/num_layers",
)
args = parser.parse_args()
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

dataset = PygNodePropPredDataset(name='ogbn-papers100M')
split_idx = dataset.get_idx_split()
if args.cugraph_data_loader:
    from cugraph.testing.mg_utils import enable_spilling
    enable_spilling()


def get_num_workers() -> int:
    try:
        return len(os.sched_getaffinity(0)) // 2
    except Exception:
        return os.cpu_count() // 2


kwargs = dict(
    num_neighbors=[args.fan_out, args.fan_out],
    batch_size=args.batch_size,
)
# Set Up Neighbor Loading
data = dataset[0]
if args.cugraph_data_loader:
    import cupy
    import rmm

    rmm.reinitialize(devices=[0], pool_allocator=True, initial_pool_size=78e9,
                     managed_memory=True)

    from rmm.allocators.torch import rmm_torch_allocator
    torch.cuda.memory.change_current_allocator(rmm_torch_allocator)

    from rmm.allocators.cupy import rmm_cupy_allocator
    cupy.cuda.set_allocator(rmm_cupy_allocator)

    import cugraph
    from cugraph_pyg.data import CuGraphStore
    from cugraph_pyg.loader import CuGraphNeighborLoader
    G = {("N", "E", "N"): data.edge_index}
    N = {"N": data.num_nodes}
    fs = cugraph.gnn.FeatureStore(backend="torch")
    fs.add_data(data.x, "N", "x")
    fs.add_data(data.y, "N", "y")
    cugraph_store = CuGraphStore(fs, G, N)
    # Note that train dataloader SHOULD have shuffle and drop_last as True.
    # However, this feature is not yet available in CuGraphNeighborLoader.
    # Coming early 2024.
    # CuGraphNeighborLoader can produce huge speed ups but not shuffling
    # can have negative impacts on val/test accuracy.
    train_loader = CuGraphNeighborLoader(
        cugraph_store,
        input_nodes=split_idx['train'],
        # shuffle=True, drop_last=True,
        **kwargs)
    val_loader = CuGraphNeighborLoader(cugraph_store,
                                       input_nodes=split_idx['valid'],
                                       **kwargs)
    test_loader = CuGraphNeighborLoader(cugraph_store,
                                        input_nodes=split_idx['test'],
                                        **kwargs)
else:
    num_work = get_num_workers()
    NeighborLoader = torch_geometric.loader.NeighborLoader
    train_loader = NeighborLoader(data=data, input_nodes=split_idx['train'],
                                  num_workers=num_work, drop_last=True,
                                  shuffle=False, **kwargs)
    val_loader = NeighborLoader(data=data, input_nodes=split_idx['valid'],
                                num_workers=num_work, **kwargs)
    test_loader = NeighborLoader(data=data, input_nodes=split_idx['test'],
                                 num_workers=num_work, **kwargs)

if args.use_gat_conv:
    model = torch_geometric.nn.models.GAT(
        dataset.num_features, args.hidden_channels, args.num_layers,
        dataset.num_classes, heads=args.n_gat_conv_heads).to(device)
else:
    model = torch_geometric.nn.models.GCN(dataset.num_features,
                                          args.hidden_channels,
                                          args.num_layers,
                                          dataset.num_classes).to(device)

optimizer = torch.optim.Adam(model.parameters(), lr=args.lr,
                             weight_decay=0.0005)

warmup_steps = 20


def train():
    model.train()
    start_timer = False
    for i, batch in enumerate(train_loader):
        if isinstance(batch, torch_geometric.data.HeteroData):
            batch = batch.to_homogeneous()

        if i == warmup_steps:
            start_avg_time = time.perf_counter()
        start = time.perf_counter()
        batch = batch.to(device)
        optimizer.zero_grad()
        batch_size = batch.num_sampled_nodes[0]
        out = model(batch.x, batch.edge_index)[:batch_size]
        y = batch.y[:batch_size].view(-1).to(torch.long)
        loss = F.cross_entropy(out, y)
        loss.backward()
        optimizer.step()

        if i % 10 == 0:
            print(f'Epoch: {epoch:02d}, Iteration: {i}, Loss: {loss:.4f}, '
                  f's/iter: {time.perf_counter() - start:.6f}')

    print(f'Average Training Iteration Time (s/iter): \
            {(time.perf_counter() - start_avg_time)/(i-warmup_steps):.6f}')


@torch.no_grad()
def test(loader, eval_steps: Optional[int] = None):
    model.eval()

    total_correct = total_examples = 0
    for i, batch in enumerate(loader):
        if eval_steps is not None and i >= eval_steps:
            break
        if isinstance(batch, torch_geometric.data.HeteroData):
            batch = batch.to_homogeneous()
        batch = batch.to(device)
        batch_size = batch.num_sampled_nodes[0]
        out = model(batch.x, batch.edge_index)[:batch_size]
        pred = out.argmax(dim=-1)
        y = batch.y[:batch_size].view(-1).to(torch.long)

        total_correct += int((pred == y).sum())
        total_examples += y.size(0)

    return total_correct / total_examples


for epoch in range(1, 1 + args.epochs):
    train()
    val_acc = test(val_loader, eval_steps=100)
    print(f'Val Acc: ~{val_acc:.4f}')

test_acc = test(test_loader)
print(f'Test Acc: {test_acc:.4f}')
