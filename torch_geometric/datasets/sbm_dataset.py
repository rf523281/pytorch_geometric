import os
from typing import Callable, List, Optional

import numpy as np
import torch
from torch import Tensor

from torch_geometric.data import Data, InMemoryDataset
from torch_geometric.utils import stochastic_blockmodel_graph


class StochasticBlockModelDataset(InMemoryDataset):
    r"""A synthetic graph dataset generated by the stochastic block model.
    The node features of each block are sampled from normal distributions where
    the centers of clusters are vertices of a hypercube, as computed by the
    :meth:`sklearn.datasets.make_classification` method.

    Args:
        root (str): Root directory where the dataset should be saved.
        block_sizes ([int] or LongTensor): The sizes of blocks.
        edge_probs ([[float]] or FloatTensor): The density of edges going from
            each block to each other block. Must be symmetric if the graph is
            undirected.
        num_channels (int, optional): The number of node features. If given
            as :obj:`None`, node features are not generated.
            (default: :obj:`None`)
        is_undirected (bool, optional): Whether the graph to generate is
            undirected. (default: :obj:`True`)
        transform (callable, optional): A function/transform that takes in
            an :obj:`torch_geometric.data.Data` object and returns a
            transformed version. The data object will be transformed before
            every access. (default: :obj:`None`)
        pre_transform (callable, optional): A function/transform that takes
            in an :obj:`torch_geometric.data.Data` object and returns a
            transformed version. The data object will be transformed
            before being saved to disk. (default: :obj:`None`)
        **kwargs (optional): The keyword arguments that are passed down to the
            :meth:`sklearn.datasets.make_classification` method for drawing
            node features.
    """
    def __init__(
        self,
        root: str,
        block_sizes: List[int] | Tensor,
        edge_probs: List[List[float]] | Tensor,
        num_channels: Optional[int] = None,
        is_undirected: bool = True,
        transform: Optional[Callable] = None,
        pre_transform: Optional[Callable] = None,
        **kwargs,
    ):
        if not isinstance(block_sizes, torch.Tensor):
            block_sizes = torch.tensor(block_sizes, dtype=torch.long)
        if not isinstance(edge_probs, torch.Tensor):
            edge_probs = torch.tensor(edge_probs, dtype=torch.float)

        self.block_sizes = block_sizes
        self.edge_probs = edge_probs
        self.num_channels = num_channels
        self.is_undirected = is_undirected

        self.kwargs = {
            'n_informative': num_channels,
            'n_redundant': 0,
            'flip_y': 0.0,
            'shuffle': False,
        }
        self.kwargs.update(kwargs)

        super().__init__(root, transform, pre_transform)
        self.data, self.slices = torch.load(self.processed_paths[0])

    @property
    def processed_dir(self) -> str:
        return os.path.join(self.root, self.__class__.__name__, 'processed')

    @property
    def processed_file_names(self) -> str:
        block_sizes = self.block_sizes.view(-1).tolist()
        hash1 = '-'.join([f'{x:.1f}' for x in block_sizes])

        edge_probs = self.edge_probs.view(-1).tolist()
        hash2 = '-'.join([f'{x:.1f}' for x in edge_probs])

        return f'data_{self.num_channels}_{hash1}_{hash2}.pt'

    def process(self):
        from sklearn.datasets import make_classification

        edge_index = stochastic_blockmodel_graph(
            self.block_sizes, self.edge_probs, directed=not self.is_undirected)

        num_samples = int(self.block_sizes.sum())
        num_classes = self.block_sizes.size(0)

        x = None
        if self.num_channels is not None:
            x, y_not_sorted = make_classification(
                n_samples=num_samples,
                n_features=self.num_channels,
                n_classes=num_classes,
                weights=self.block_sizes / num_samples,
                **self.kwargs,
            )
            x = x[np.argsort(y_not_sorted)]
            x = torch.from_numpy(x).to(torch.float)

        y = torch.arange(num_classes).repeat_interleave(self.block_sizes)

        data = Data(x=x, edge_index=edge_index, y=y)

        if self.pre_transform is not None:
            data = self.pre_transform(data)

        torch.save(self.collate([data]), self.processed_paths[0])


class RandomPartitionGraphDataset(StochasticBlockModelDataset):
    r"""The random partition graph dataset from the `"How to Find Your
    Friendly Neighborhood: Graph Attention Design with Self-Supervision"
    <https://openreview.net/forum?id=Wi5KUNlqWty>`_ paper.
    This is a synthetic graph of communities controlled by the node homophily
    and the average degree, and each community is considered as a class.
    The node features are sampled from normal distributions where the centers
    of clusters are vertices of a hypercube, as computed by the
    :meth:`sklearn.datasets.make_classification` method.

    Args:
        root (str): Root directory where the dataset should be saved.
        num_classes (int): The number of classes.
        num_nodes_per_class (int): The number of nodes per class.
        node_homophily_ratio (float): The degree of node homophily.
        average_degree (float): The average degree of the graph.
        num_channels (int, optional): The number of node features. If given
            as :obj:`None`, node features are not generated.
            (default: :obj:`None`)
        is_undirected (bool, optional): Whether the graph to generate is
            undirected. (default: :obj:`True`)
        transform (callable, optional): A function/transform that takes in
            an :obj:`torch_geometric.data.Data` object and returns a
            transformed version. The data object will be transformed before
            every access. (default: :obj:`None`)
        pre_transform (callable, optional): A function/transform that takes
            in an :obj:`torch_geometric.data.Data` object and returns a
            transformed version. The data object will be transformed before
            being saved to disk. (default: :obj:`None`)
        **kwargs (optional): The keyword arguments that are passed down
            to :meth:`sklearn.datasets.make_classification` method in
            drawing node features.
    """
    def __init__(self, root, num_classes: int, num_nodes_per_class: int,
                 node_homophily_ratio: float, average_degree: float,
                 num_channels: Optional[int] = None,
                 is_undirected: bool = True,
                 transform: Optional[Callable] = None,
                 pre_transform: Optional[Callable] = None, **kwargs):

        self._num_classes = num_classes
        self.num_nodes_per_class = num_nodes_per_class
        self.node_homophily_ratio = node_homophily_ratio
        self.average_degree = average_degree

        # (p_in + (C - 1) * p_out) / C = |E|/|V|^2
        # i.e., p_in + (C - 1) * p_out = average_degree / num_nodes_per_class
        ec_over_v2 = average_degree / num_nodes_per_class
        p_in = node_homophily_ratio * ec_over_v2
        p_out = (ec_over_v2 - p_in) / (num_classes - 1)

        block_sizes = [num_nodes_per_class for _ in range(num_classes)]
        edge_probs = [[p_out for _ in range(num_classes)]
                      for _ in range(num_classes)]
        for r in range(num_classes):
            edge_probs[r][r] = p_in

        super().__init__(root, block_sizes, edge_probs, num_channels,
                         is_undirected, transform, pre_transform, **kwargs)
        self.data, self.slices = torch.load(self.processed_paths[0])

    @property
    def processed_file_names(self) -> str:
        return (f'data_{self.num_channels}_{self._num_classes}_'
                f'{self.num_nodes_per_class}_{self.node_homophily_ratio:.1f}_'
                f'{self.average_degree:.1f}.pt')

    def process(self):
        return super().process()
