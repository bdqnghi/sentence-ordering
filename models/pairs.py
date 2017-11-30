

import attr
import os
import click
import torch
import ujson
import numpy as np

from gensim.models import KeyedVectors
from boltons.iterutils import pairwise, chunked_iter
from tqdm import tqdm
from glob import glob
from itertools import islice

from sklearn.metrics import classification_report, accuracy_score

from torch import nn
from torch.nn import functional as F
from torch.autograd import Variable


cuda = bool(os.environ.get('CUDA'))
dtype = torch.cuda.FloatTensor if cuda else torch.FloatTensor


def load_vectors(path):
    print('Loading vectors...')
    global vectors
    vectors = KeyedVectors.load(path)


def read_abstracts(path):
    for path in glob(os.path.join(path, '*.json')):
        with open(path) as fh:
            for line in fh:
                raw = ujson.loads(line.strip())
                yield Abstract.from_raw(raw)


class Corpus:

    def __init__(self, path, skim):
        reader = islice(read_abstracts(path), skim)
        self.abstracts = list(tqdm(reader, total=skim))

    def abstract_batches(self, size):
        for chunk in chunked_iter(tqdm(self.abstracts), size):
            yield AbstractBatch(chunk)


@attr.s
class Abstract:

    sentences = attr.ib()

    @classmethod
    def from_raw(cls, raw):
        return cls([Sentence(s['token']) for s in raw['sentences']])

    def tensor(self):
        tensors = [s.tensor() for s in self.sentences]
        return torch.stack(tensors)


@attr.s
class Sentence:

    tokens = attr.ib()

    def tensor(self, dim=300, pad=50):
        x = [vectors[t] for t in self.tokens if t in vectors]
        x += [np.zeros(dim)] * pad
        x = x[:pad]
        x = list(reversed(x))
        x = np.array(x)
        x = torch.from_numpy(x)
        x = x.float()
        return x


@attr.s
class AbstractBatch:

    abstracts = attr.ib()

    def tensor(self):
        tensors = [a.tensor() for a in self.abstracts]
        return torch.cat(tensors).type(dtype)

    def xy(self, encoded_sents):

        x, y = [], []

        start = 0
        for ab in self.abstracts:
            sents = encoded_sents[start:start+len(ab.sentences)]
            for s1, s2 in pairwise(sents):

                # Correct.
                x.append(torch.cat([s1, s2]))
                y.append(1)

                # Incorrect.
                x.append(torch.cat([s2, s1]))
                y.append(0)

            start += len(ab.sentences)

        x = torch.stack(x).type(dtype)
        y = torch.FloatTensor(y).type(dtype)

        return x, y


class SentenceEncoder(nn.Module):

    def __init__(self, lstm_dim=128):
        super().__init__()
        self.lstm_dim = lstm_dim
        self.lstm = nn.LSTM(300, lstm_dim, batch_first=True)

    def forward(self, x):
        h0 = Variable(torch.zeros(1, len(x), self.lstm_dim).type(dtype))
        c0 = Variable(torch.zeros(1, len(x), self.lstm_dim).type(dtype))
        _, (hn, cn) = self.lstm(x, (h0, c0))
        return hn


class Model(nn.Module):

    def __init__(self, lstm_dim=128, lin_dim=128):
        super().__init__()
        self.lin1 = nn.Linear(2*lstm_dim, lin_dim)
        self.lin2 = nn.Linear(lin_dim, lin_dim)
        self.lin3 = nn.Linear(lin_dim, lin_dim)
        self.out = nn.Linear(lin_dim, 1)

    def forward(self, x):
        y = F.relu(self.lin1(x))
        y = F.relu(self.lin2(y))
        y = F.relu(self.lin3(y))
        y = F.sigmoid(self.out(y))
        return y


@click.command()
@click.argument('train_path', type=click.Path())
@click.argument('test_path', type=click.Path())
@click.argument('vectors_path', type=click.Path())
@click.option('--train_skim', type=int, default=10000)
@click.option('--test_skim', type=int, default=1000)
@click.option('--lr', type=float, default=1e-2)
@click.option('--epochs', type=int, default=10)
@click.option('--batch_size', type=int, default=50)
@click.option('--lstm_dim', type=int, default=128)
@click.option('--lin_dim', type=int, default=128)
def main(train_path, test_path, vectors_path, train_skim, test_skim,
    lr, epochs, batch_size, lstm_dim, lin_dim):

    load_vectors(vectors_path)

    # TRAIN

    torch.manual_seed(1)
    train = Corpus(train_path, train_skim)

    sent_encoder = SentenceEncoder(lstm_dim)
    model = Model(lstm_dim, lin_dim)

    if cuda:
        sent_encoder.cuda()
        model.cuda()

    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    criterion = nn.BCELoss()

    train_loss = []
    for epoch in range(epochs):

        print(f'\nEpoch {epoch}')

        epoch_loss = 0
        for batch in train.abstract_batches(batch_size):

            sent_encoder.zero_grad()
            model.zero_grad()

            sents = Variable(batch.tensor())
            sents = sent_encoder(sents)

            x, y = batch.xy(sents.squeeze())
            y = Variable(y)

            y_pred = model(x)
            y_pred = y_pred.view(-1)

            loss = criterion(y_pred, y)
            loss.backward()

            optimizer.step()

            epoch_loss += loss.data[0]

        epoch_loss /= train_skim
        train_loss.append(epoch_loss)
        print(epoch_loss)

    # EVAL

    test = Corpus(test_path, test_skim)

    yt = []
    yp = []
    for batch in test.abstract_batches(batch_size):

        sents = Variable(batch.tensor())
        sents = sent_encoder(sents)

        x, y = batch.xy(sents.squeeze())
        y = Variable(y)

        y_pred = model(x)
        y_pred = y_pred.view(-1)

        yt += y.data.type(torch.ByteTensor).tolist()
        yp += y_pred.round().data.type(torch.ByteTensor).tolist()

    print(classification_report(yt, yp))
    print('Accuracy: %f' % accuracy_score(yt, yp))


if __name__ == '__main__':
    main()
