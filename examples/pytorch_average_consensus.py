# Copyright 2020 Bluefog Team. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
import argparse

import torch
import numpy as np
import bluefog.torch as bf
from bluefog.common import topology_util

parser = argparse.ArgumentParser(description='PyTorch Average Consensus',
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('--data-size', type=int, default=100000,
                    help='the size of data.')
parser.add_argument('--max-iters', type=int, default=200,
                    help='maximum iterations')
parser.add_argument('--virtual-topology', type=str, default="power2",
                    help='The underlying virtual topology. Supporting options are ' +
                    '[power2(Default), ring, mesh, star].')
parser.add_argument('--asynchronous-mode', action='store_true', default=False,
                    help='Use one-sided ops to run asynchronous push sum algorithm')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='disables CUDA training')
parser.add_argument('--enable-dynamic-topology', action='store_true',
                    default=False, help=('Enable each iteration to transmit one neighbor ' +
                                         'per iteration dynamically.'))
parser.add_argument(
    "--plot-interactive", action='store_true', help="Use plt.show() to present the plot."
)
parser.add_argument('--seed', type=int, default=2020,
                    help='Seed for randomness.')

args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

bf.init()

torch.random.manual_seed(args.seed * bf.rank())
if args.cuda:
    device = bf.local_rank() %  torch.cuda.device_count()
    x = torch.randn(args.data_size, device=device, dtype=torch.double)
else:
    x = torch.randn(args.data_size, dtype=torch.double)

if args.virtual_topology == "power2":
    pass
elif args.virtual_topology == "power3":
    bf.set_topology(topology_util.PowerGraph(bf.size(), base=3))
elif args.virtual_topology == "power4":
    bf.set_topology(topology_util.PowerGraph(bf.size(), base=4))
elif args.virtual_topology == "ring":
    bf.set_topology(topology_util.RingGraph(bf.size(), connect_style=0))
elif args.virtual_topology == "mesh":
    bf.set_topology(topology_util.RingGraph(
        bf.size(), connect_style=0), is_weighted=True)
elif args.virtual_topology == "star":
    bf.set_topology(topology_util.StarGraph(bf.size()), is_weighted=True)
elif args.virtual_topology == "full":
    bf.set_topology(topology_util.FullyConnectedGraph(bf.size()))
else:
    raise ValueError("Unknown args.virtual_topology, supporting options are " +
                     "[power2(Default), ring, mesh, star].")

x_bar = bf.allreduce(x, average=True)
mse = [torch.norm(x-x_bar, p=2) / torch.norm(x_bar, p=2)]

if not args.asynchronous_mode:
    self_weight = None
    neighbor_weights = None
    send_neighbors = None

    if args.enable_dynamic_topology:
        dynamic_neighbor_allreduce_gen = topology_util.GetDynamicSendRecvRanks(
            bf.load_topology(), bf.rank())

    for _ in range(args.max_iters):
        if args.enable_dynamic_topology:
            send_neighbor, recv_neighbors = next(dynamic_neighbor_allreduce_gen)
            send_neighbors = [send_neighbor]
            neighbor_weights = {
                r: 1/(len(recv_neighbors) + 1) for r in recv_neighbors}
            self_weight = 1 / (len(recv_neighbors) + 1)

        x = bf.neighbor_allreduce(x, name='x', self_weight=self_weight,
                                  neighbor_weights=neighbor_weights,
                                  send_neighbors=send_neighbors, enable_topo_check=False)
        mse.append(torch.norm(x-x_bar, p=2) / torch.norm(x_bar, p=2))
else:
    outdegree = len(bf.out_neighbor_ranks())
    indegree = len(bf.in_neighbor_ranks())
    # For push-sum algorithm we need extra scalar p to associated with data x.
    p = torch.DoubleTensor([1.0]).to(x.device)
    x_ext = torch.cat([x, p], 0)

    bf.win_create(x_ext, name="x_ext", zero_init=True)
    for i in range(args.max_iters):
        if args.enable_dynamic_topology:
            num_out_neighbors = len(bf.out_neighbor_ranks())
            sent_neighbor = bf.out_neighbor_ranks()[i % num_out_neighbors]
            dst_weights = {sent_neighbor: 0.5}
            self_weight = 0.5
        else:
            dst_weights = {rank: 1.0 / (outdegree + 1)
                           for rank in bf.out_neighbor_ranks()}
            self_weight = 1/(1+outdegree)

        # Out-going neighbor
        bf.win_accumulate(x_ext, name="x_ext",
                          dst_weights=dst_weights, require_mutex=True)
        # Self times weight.
        x_ext.mul_(self_weight)

        bf.win_update_then_collect(name="x_ext")
        mse.append(torch.norm(x_ext[:-1]/x_ext[-1]-x_bar, p=2) / torch.norm(x_bar, p=2))

    # Do not forget to sync at last!
    bf.barrier()
    bf.win_update_then_collect(name="x_ext")

    mse.append(torch.norm(x_ext[:-1]/x_ext[-1]-x_bar, p=2) / torch.norm(x_bar, p=2))
    p_push_sum = bf.allreduce(x_ext[-1], average=True)
    if bf.rank() == 0:
        print("Total Sum of p should be 1, always", p_push_sum)
    bf.win_free(name="x_ext")


print("MSE at last iteration: ", mse[-1])
if args.plot_interactive and bf.rank() == 0:
    import matplotlib.pyplot as plt
    plt.semilogy(mse)
    plt.show()
