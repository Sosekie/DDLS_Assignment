import torch
from torch.utils.data import DataLoader
from torchvision import datasets, transforms

data_path_str = "./data"
ETA = "\N{GREEK SMALL LETTER ETA}"
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
# device = torch.device("cpu")
torch.backends.cudnn.deterministic=True

print(device)

transform = transforms.Compose([
    transforms.ToTensor(),
    # normalize by training set mean and standard deviation
    # resulting data has mean=0 and std=1
    transforms.Normalize((0.1307,), (0.3081,))
])

train_dataset = datasets.MNIST(data_path_str, train=True, download=True, transform=transform)

# why set test_loader as global variable
test_loader = DataLoader(
    datasets.MNIST(data_path_str, train=False, download=False, transform=transform),
    # decrease batch size if running into memory issues when testing
    # a bespoke generator is passed to avoid reproducibility issues
    shuffle=False, drop_last=False, batch_size=10000, generator=torch.Generator())

#

import torch.nn as nn
import torch.nn.functional as F


class MnistCnn(nn.Module):
    def __init__(self):
        super(MnistCnn, self).__init__()

        self.conv1 = nn.Conv2d(1, 32, 3, 1)
        self.conv2 = nn.Conv2d(32, 64, 3, 1)
        self.dropout1 = nn.Dropout(0.25)
        self.dropout2 = nn.Dropout(0.5)
        self.fc1 = nn.Linear(9216, 128)
        self.fc2 = nn.Linear(128, 10)

    def forward(self, x):
        x = self.conv1(x)
        x = F.relu(x)
        x = self.conv2(x)
        x = F.relu(x)
        x = F.max_pool2d(x, 2)
        x = self.dropout1(x)
        x = torch.flatten(x, 1)
        x = self.fc1(x)
        x = F.relu(x)
        x = self.dropout2(x)
        x = self.fc2(x)
        output = F.log_softmax(x, dim=1)

        return output

#

from torch.optim import Optimizer

# In train_epoch, feedforward, backward, update

def train_epoch(model: torch.nn.Module, loader: DataLoader, optimizer: Optimizer) -> None:
    model.train()

    for data, target in loader:
        data, target = data.to(device), target.to(device)
        optimizer.zero_grad()
        output = model(data)
        loss = F.nll_loss(output, target)
        loss.backward()
        optimizer.step()

#

from typing import cast

import numpy as np
import numpy.random as npr
from torch.utils.data import Subset


def split(nr_clients: int, iid: bool, seed: int) -> list[Subset]:
    rng = npr.default_rng(seed)

    if iid:
        splits = np.array_split(rng.permutation(len(train_dataset)), nr_clients)
    # manually create niid dataset, using sorted targets
    else:
        # sorted_indices: ascending order index : [5, 3, ...]
        sorted_indices = np.argsort(np.array([target for _data, target in train_dataset]))
        # len(shards) = 2 * nr_clients
        shards = np.array_split(sorted_indices, 2 * nr_clients)
        shuffled_shard_indices = rng.permutation(len(shards))
        splits = [
            np.concatenate([shards[i] for i in inds], dtype=np.int64)
            for inds in shuffled_shard_indices.reshape(-1, 2)]

    return [Subset(train_dataset, split) for split in cast(list[list[int]], splits)]

#

from dataclasses import asdict, dataclass, field

from pandas import DataFrame


@dataclass
class RunResult:
    algorithm: str
    n: int  # number of clients
    c: float  # client_fraction
    b: int  # take -1 as inf
    e: int  # nr_local_epochs
    lr: float  # printed as lowercase eta
    seed: int
    wall_time: list[float] = field(default_factory=list)
    message_count: list[int] = field(default_factory=list)
    test_accuracy: list[float] = field(default_factory=list)

    # self means this dataclass
    # -> followed by a type annotation to specify the return type of a function or method
    def as_df(self, skip_wtime=False) -> DataFrame:
        self_dict = {
            # Capitalize the first letter of each key (attribute name) and replace underscores with spaces
            k.capitalize().replace("_", " "): v
            for k, v in asdict(self).items()}

        if self_dict["B"] == -1:
            self_dict["B"] = "\N{INFINITY}"

        df = DataFrame({"Round": range(1, len(self.wall_time) + 1), **self_dict})
        df = df.rename(columns={"Lr": ETA})
        if skip_wtime:
            df = df.drop(columns=["Wall time"])
        return df

#

from abc import ABC, abstractmethod

# ABC: Abstract Base Class
class Client(ABC):
    def __init__(self, client_data: Subset, batch_size: int) -> None:
        self.model = MnistCnn().to(device)
        self.generator = torch.Generator()
        self.loader_train = DataLoader(
            client_data, batch_size=batch_size, shuffle=True,
            drop_last=False, generator=self.generator)

    # This means that any subclass inheriting from Client must implement the update method, 
    # otherwise the subclass is also considered abstract and cannot be instantiated.
    @abstractmethod
    def update(self, weights: list[torch.Tensor], seed: int) -> list[torch.Tensor]:
        ...

#

class Server(ABC):
    def __init__(self, lr: float, batch_size: int, seed: int) -> None:
        self.clients: list[Client]
        self.lr = lr
        self.batch_size = batch_size
        self.seed = seed
        torch.manual_seed(seed)
        self.model = MnistCnn().to(device)
        # we have self.loader_train in Client, why set test_loader as global


    @abstractmethod
    def run(self, nr_rounds: int) -> RunResult:
        ...


    def test(self) -> float:
        correct = 0
        self.model.eval()

        with torch.no_grad():
            for data, target in test_loader:
                data, target = data.to(device), target.to(device)
                output = self.model(data)
                pred = output.argmax(dim=1, keepdim=True)
                correct += pred.eq(target.view_as(pred)).sum().item()

        return 100. * correct / len(cast(datasets.MNIST, test_loader.dataset))

#

from time import perf_counter

from torch.optim import SGD
from tqdm import tqdm


class CentralizedServer(Server):
    def __init__(self, lr: float, batch_size: int, seed: int) -> None:
        super().__init__(lr, batch_size, seed)
        self.optimizer = SGD(params=self.model.parameters(), lr=lr)
        self.generator = torch.Generator()
        self.loader_train = DataLoader(
            train_dataset, batch_size=batch_size, shuffle=True,
            drop_last=False, generator=self.generator)
        self.clients = []

    def run(self, nr_rounds: int) -> RunResult:
        elapsed_time = 0.
        run_result = RunResult("Centralized", 1, 1, self.batch_size, 1, self.lr, self.seed)
        # algorithm, n: number of clients, c: client_fraction, b: batchsize, e: nr_local_epochs, lr, seed, wall_time, message_count, test_accuracy

        for epoch in tqdm(range(nr_rounds), desc="Epochs", leave=False):
            # Record the current time (floating point number in seconds)
            start_time = perf_counter()
            # Ensure that each round of random number generation is reproducible while being slightly different
            self.generator.manual_seed(self.seed + epoch + 1)
            # train here, including backward and update
            train_epoch(self.model, self.loader_train, self.optimizer)
            elapsed_time += perf_counter() - start_time
            run_result.wall_time.append(round(elapsed_time, 1))
            run_result.message_count.append(0)
            # use Server.test to get result, no need for Declarative Test Sets
            run_result.test_accuracy.append(self.test())

        return run_result

#

class DecentralizedServer(Server):
    def __init__(self, lr: float, batch_size: int, client_subsets: list[Subset], client_fraction: float, seed: int) -> None:
        super().__init__(lr, batch_size, seed)
        self.nr_clients = len(client_subsets)
        # Specify the percentage of clients that should be activated and used for training in each training round
        self.client_fraction = client_fraction
        # Allows the server to know how much data each client has
        self.client_sample_counts = [len(subset) for subset in client_subsets]
        # Use max to ensure that at least one client is selected, even if the result of the calculation is less than 1.
        self.nr_clients_per_round = max(1, round(client_fraction * self.nr_clients))
        self.rng = npr.default_rng(seed)

# ---

class GradientClient(Client):
    def __init__(self, client_data: Subset) -> None:
        # Client: (self, client_data: Subset, batch_size: int)
        # here, batch_size = len(client_data)
        super().__init__(client_data, len(client_data))
        # Why there is no nr_epochs? - we train on total data, and got gradients already, no need for re calculate

    # weights are from server, return gradients not weights from local model
    # Why no grad_zero in client: server's weight take place of clients, so we do not need to zero_grad?
    def update(self, weights: list[torch.Tensor], seed: int) -> list[torch.Tensor]:
        with torch.no_grad():
            for client_values, server_values in zip(self.model.parameters(), weights):
                client_values[:] = server_values
                client_values.grad = None

        # seeding is not strictly necessary here
        self.generator.manual_seed(seed)
        self.model.train()

        # this will always have one iteration, since batchsize is len(total)
        # Why?
        # More stable convergence, 
        # each commit from the client is a best-effort update based on all of its data.
        for data, target in self.loader_train:
            data, target = data.to(device), target.to(device)
            output = self.model(data)
            loss = F.nll_loss(output, target)
            # after backward, each node has grad
            loss.backward()

        return [
            # return gradients
            cast(torch.Tensor, x.grad).detach().cpu().clone()
            for x in self.model.parameters()]

#

class FedSgdGradientServer(DecentralizedServer):
    def __init__(
            self, lr: float,
            client_subsets: list[Subset], client_fraction: float, seed: int) -> None:
        super().__init__(lr, -1, client_subsets, client_fraction, seed)
        self.optimizer = SGD(params=self.model.parameters(), lr=lr)
        # List-deductive traversal of these subsets creates a GradientClient instance for each of them
        self.clients = [GradientClient(subset) for subset in client_subsets]

    def run(self, nr_rounds: int) -> RunResult:
        elapsed_time = 0.
        run_result = RunResult("FedSGDGradient", self.nr_clients, self.client_fraction, -1, 1, self.lr, self.seed)

        for nr_round in tqdm(range(nr_rounds), desc="Rounds", leave=False):
            setup_start_time = perf_counter()
            self.model.train()
            self.optimizer.zero_grad()
            # this weight from Server
            weights = [x.detach().cpu().clone() for x in self.model.parameters()]
            # Randomly selects the specified number (self.nr_clients_per_round) of unique elements from the given range (0 to self.nr_clients-1).
            # replace=False: this parameter specifies that there should be no duplicates in the selection
            indices_chosen_clients = self.rng.choice(self.nr_clients, self.nr_clients_per_round, replace=False)
            chosen_sum_nr_samples = sum(self.client_sample_counts[i] for i in indices_chosen_clients)
            # store new weights
            chosen_adjusted_gradients: list[list[torch.Tensor]] = []
            elapsed_time += perf_counter() - setup_start_time
            update_time = 0.

            for c_i in indices_chosen_clients:
                update_start_time = perf_counter()
                ind = int(c_i)
                # why add nr_round * self.nr_clients_per_round?
                client_round_seed = self.seed + ind + 1 + nr_round * self.nr_clients_per_round
                # client_round_seed for dataset randomization
                # client_gradients: list of clients gradients
                client_gradients = self.clients[ind].update(weights, client_round_seed)
                chosen_adjusted_gradients.append([
                    # The amount of data for each client was calculated as a percentage of the total amount of data for all selected clients.
                    self.client_sample_counts[ind] / chosen_sum_nr_samples * tens
                     for tens in client_gradients])
                update_time = max(update_time, perf_counter() - update_start_time)

            elapsed_time += update_time
            aggregate_start_time = perf_counter()
            averaged_chosen_gradients: list[torch.Tensor] = [sum(x) for x in zip(*chosen_adjusted_gradients)]

            with torch.no_grad():
                for client_gradient, server_parameter in zip(averaged_chosen_gradients, self.model.parameters()):
                    server_parameter.grad = client_gradient.to(device=device)

            # now update server weight
            self.optimizer.step()
            elapsed_time += perf_counter() - aggregate_start_time
            run_result.wall_time.append(round(elapsed_time, 1))
            run_result.message_count.append(2 * (nr_round + 1) * self.nr_clients_per_round)
            run_result.test_accuracy.append(self.test())

        return run_result

#

class WeightClient(Client):
    def __init__(self, client_data: Subset, lr: float, batch_size: int, nr_epochs: int) -> None:
        super().__init__(client_data, batch_size)
        self.optimizer = SGD(params=self.model.parameters(), lr=lr)
        self.nr_epochs = nr_epochs


    def update(self, weights: list[torch.Tensor], seed: int) -> list[torch.Tensor]:
        with torch.no_grad():
            for client_values, server_values in zip(self.model.parameters(), weights):
                client_values[:] = server_values

        self.generator.manual_seed(seed)

        # why here we need multi epoch?
        for _epoch in range(self.nr_epochs):
            train_epoch(self.model, self.loader_train, self.optimizer)

        # not gradients this time
        return [x.detach().cpu().clone() for x in self.model.parameters()]

#

class FedAvgServer(DecentralizedServer):
    def __init__(
            self, lr: float, batch_size: int, client_subsets: list[Subset],
            client_fraction: float, nr_local_epochs: int, seed: int) -> None:
        super().__init__(lr, batch_size, client_subsets, client_fraction, seed)
        self.name = "FedAvg"
        self.nr_local_epochs = nr_local_epochs
        self.clients = [
            WeightClient(subset, lr, batch_size, nr_local_epochs)
            for subset in client_subsets]

    def run(self, nr_rounds: int) -> RunResult:
        elapsed_time = 0.
        run_result = RunResult(self.name, self.nr_clients, self.client_fraction, self.batch_size, self.nr_local_epochs, self.lr, self.seed)

        for nr_round in tqdm(range(nr_rounds), desc="Rounds", leave=False):
            setup_start_time = perf_counter()
            self.model.train()
            weights = [x.detach().cpu().clone() for x in self.model.parameters()]
            indices_chosen_clients = self.rng.choice(self.nr_clients, self.nr_clients_per_round, replace=False)
            chosen_sum_nr_samples = sum(self.client_sample_counts[i] for i in indices_chosen_clients)
            chosen_adjusted_weights: list[list[torch.Tensor]] = []
            elapsed_time += perf_counter() - setup_start_time
            update_time = 0.

            for c_i in indices_chosen_clients:
                update_start_time = perf_counter()
                ind = int(c_i)
                client_round_seed = self.seed + ind + 1 + nr_round * self.nr_clients_per_round
                client_weights = self.clients[ind].update(weights, client_round_seed)
                chosen_adjusted_weights.append([
                    self.client_sample_counts[ind] / chosen_sum_nr_samples * tens
                     for tens in client_weights])
                update_time = max(update_time, perf_counter() - update_start_time)

            elapsed_time += update_time
            aggregate_start_time = perf_counter()
            averaged_chosen_weights: list[torch.Tensor] = [sum(x) for x in zip(*chosen_adjusted_weights)]

            with torch.no_grad():
                for client_weight, server_parameter in zip(averaged_chosen_weights, self.model.parameters()):
                    server_parameter[:] = client_weight.to(device=device)

            elapsed_time += perf_counter() - aggregate_start_time
            run_result.wall_time.append(round(elapsed_time, 1))
            run_result.message_count.append(2 * (nr_round + 1) * self.nr_clients_per_round)
            run_result.test_accuracy.append(self.test())

        return run_result
