# Modifications copyright (C) 2020 Bluefog Team. All Rights Reserved.
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
import timeit

import numpy as np
import torch
import torch.backends.cudnn as cudnn
from torch import nn
import torch.nn.functional as F
import torch.optim as optim
import torch.utils.data.distributed
from torchvision import models
import bluefog.torch as bf


# Benchmark settings
parser = argparse.ArgumentParser(description='PyTorch Synthetic Benchmark',
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)

parser.add_argument('--model', type=str, default='resnet50',
                    help='model to benchmark')
parser.add_argument('--batch-size', type=int, default=32,
                    help='input batch size')

parser.add_argument('--num-warmup-batches', type=int, default=10,
                    help='number of warm-up batches that don\'t count towards benchmark')
parser.add_argument('--num-batches-per-iter', type=int, default=10,
                    help='number of batches per benchmark iteration')
parser.add_argument('--num-iters', type=int, default=10,
                    help='number of benchmark iterations')
parser.add_argument('--num-classes', type=int, default=1000,
                    help='number of classes')

parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='disables CUDA training')
parser.add_argument('--profiler', action='store_true', default=False,
                    help='disables profiler')
parser.add_argument('--partition', type=int, default=None,
                    help='partition size')
parser.add_argument('--dist-optimizer', type=str, default='win_put',
                    help='The type of distributed optimizer. Supporting options are '+
                    '[win_put, neighbor_allreduce, allreduce, pull_get, push_sum, horovod]')
parser.add_argument('--enable-dynamic-topology', action='store_true',
                    default=False, help=('Enable each iteration to transmit one neighbor ' +
                                         'per iteration dynamically.'))


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

if args.dist_optimizer == 'horovod':
    print("importing horovod")
    import horovod.torch as bf

bf.init()

if args.cuda:
    torch.cuda.set_device(bf.local_rank() % torch.cuda.device_count())
    cudnn.benchmark = True

# Set up standard model.
if args.model == "lenet":
    # lenet for cpu test only.
    class LeNet(nn.Module):
        def __init__(self):
            super(LeNet, self).__init__()
            self.conv1 = nn.Conv2d(1, 10, kernel_size=5)
            self.conv2 = nn.Conv2d(10, 20, kernel_size=5)
            self.conv2_drop = nn.Dropout2d()
            self.fc1 = nn.Linear(320, 50)
            self.fc2 = nn.Linear(50, 10)

        def forward(self, x):
            x = F.relu(F.max_pool2d(self.conv1(x), 2))
            x = F.relu(F.max_pool2d(self.conv2_drop(self.conv2(x)), 2))
            x = x.view(-1, 320)
            x = F.relu(self.fc1(x))
            x = F.dropout(x, training=self.training)
            x = self.fc2(x)
            return F.log_softmax(x, dim=0)
    model = LeNet()
else:
    model = getattr(models, args.model)(num_classes=args.num_classes)

if args.cuda:
    # Move model to GPU.
    model.cuda()

optimizer = optim.SGD(model.parameters(), lr=0.01)

# Bluefog: wrap optimizer with DistributedOptimizer.
if args.dist_optimizer == 'win_put':
    optimizer = bf.DistributedBluefogOptimizer(optimizer, model=model)
elif args.dist_optimizer == 'neighbor_allreduce':
    optimizer = optimizer = bf.DistributedNeighborAllreduceOptimizer(
        optimizer, model=model)
elif args.dist_optimizer == 'allreduce':
    optimizer = optimizer = bf.DistributedAllreduceOptimizer(
        optimizer, model=model)
elif args.dist_optimizer == 'push_sum':
    optimizer = bf.DistributedPushSumOptimizer(optimizer, model=model)
elif args.dist_optimizer == 'horovod':
    optimizer = optimizer = bf.DistributedOptimizer(
        optimizer, named_parameters=model.named_parameters()
    )
elif args.dist_optimizer == 'pull_get':
    optimizer = bf.DistributedPullGetOptimizer(optimizer, model=model)
else:
    raise ValueError('Unknown args.dist-optimizer type -- ' + args.dist_optimizer + '\n' +
                     'Please set the argument to be one of ' +
                     '[win_put, neighbor_allreduce, allreduce, push_sum, horovod]')

bf.broadcast_parameters(model.state_dict(), root_rank=0)
bf.broadcast_optimizer_state(optimizer, root_rank=0)

# Set up fake data
datasets = []
for _ in range(100):
    # First two should be CPU usage only.
    if args.model == "lenet":
        data = torch.rand(args.batch_size, 1, 28, 28)  # mnist size
        target = torch.LongTensor(args.batch_size).random_() % 10
    elif args.model == 'resnet18':
        data = torch.rand(args.batch_size, 3, 32, 32)  # CIFAR10 size
        target = torch.LongTensor(args.batch_size).random_() % 10
    else:
        data = torch.rand(args.batch_size, 3, 224, 224)
        target = torch.LongTensor(args.batch_size).random_() % 1000
    if args.cuda:
        data, target = data.cuda(), target.cuda()
    datasets.append(data)
data_index = 0


def dynamic_topology_update(batch_idx):
    if args.dist_optimizer == 'win_put':
        num_out_neighbors = len(bf.out_neighbor_ranks())
        sent_neighbor = bf.out_neighbor_ranks()[batch_idx % num_out_neighbors]
        optimizer.dst_weights = {sent_neighbor: 1.0}
    elif args.dist_optimizer == 'pull_get':
        num_in_neighbors = len(bf.in_neighbor_ranks())
        recv_neighbor = bf.in_neighbor_ranks()[batch_idx % num_in_neighbors]
        optimizer.src_weights = {recv_neighbor: 1.0}
    else:
        pass


def benchmark_step():
    global data_index

    if args.enable_dynamic_topology:
        dynamic_topology_update(data_index)
    data = datasets[data_index % len(datasets)]
    data_index += 1
    optimizer.zero_grad()
    output = model(data)
    loss = F.cross_entropy(output, target)
    loss.backward()
    optimizer.step()


def log(s, nl=True):
    if bf.local_rank() != 0:
        return
    print(s, end='\n' if nl else '', flush=True)


log('Model: %s' % args.model)
log('Batch size: %d' % args.batch_size)
device = 'GPU' if args.cuda else 'CPU'
log('Number of %ss: %d' % (device, bf.size()))

# Warm-up
log('Running warmup...')
timeit.timeit(benchmark_step, number=args.num_warmup_batches)

# Benchmark
log('Running benchmark...')
img_secs = []
enable_profiling = args.profiler & (bf.rank() == 0)

with torch.autograd.profiler.profile(enable_profiling, True) as prof:
    for x in range(args.num_iters):
        time = timeit.timeit(benchmark_step, number=args.num_batches_per_iter)
        img_sec = args.batch_size * args.num_batches_per_iter / time
        log('Iter #%d: %.1f img/sec per %s' % (x, img_sec, device))
        img_secs.append(img_sec)


# Results
img_sec_mean = np.mean(img_secs)
img_sec_conf = 1.96 * np.std(img_secs)
img_secs_sum = bf.allreduce(torch.from_numpy(
    np.array(img_secs)), average=False)
img_sec_mean_all = np.mean(img_secs_sum.numpy())
img_sec_conf_all = 1.96 * np.std(img_secs_sum.numpy())
print('[%d] Img/sec per %s: %.1f +-%.1f' %
      (bf.rank(), device, img_sec_mean, img_sec_conf))
log('Total img/sec on %d %s(s): %.1f +-%.1f' %
    (bf.size(), device, img_sec_mean_all, img_sec_conf_all))