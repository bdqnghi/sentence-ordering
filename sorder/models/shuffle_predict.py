

import numpy as np

import attr
import os
import click
import torch
import ujson

from gensim.models import KeyedVectors
from boltons.iterutils import pairwise, chunked_iter
from tqdm import tqdm
from glob import glob
from itertools import islice
from scipy import stats

from sklearn.metrics import classification_report, accuracy_score

from torch import nn
from torch.nn import functional as F
from torch.autograd import Variable

from sorder.perms import sample_uniform_perms


cuda = bool(os.environ.get('CUDA'))
ftype = torch.cuda.FloatTensor if cuda else torch.FloatTensor
itype = torch.cuda.LongTensor if cuda else torch.LongTensor


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

    def batches(self, size):
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
        return torch.cat(tensors)

    def unpack_encoded_batch(self, batch):
        start = 0
        for ab in self.abstracts:
            yield batch[start:start+len(ab.sentences)]
            start += len(ab.sentences)

    def xy(self, batch, maxlen=10):
        for sents in self.unpack_encoded_batch(batch):

            if len(sents) >= maxlen:
                continue

            # Get zeros padding tensor, to normalize abstract lengths.
            pad_dim = sents.data.shape[1]
            pad_len = maxlen-len(sents)
            zeros = Variable(torch.zeros(pad_len, pad_dim)).type(ftype)

            # Correct order.
            yield (
                torch.cat([zeros, sents]),
                Variable(torch.FloatTensor([1]))
            )

            shuffle = torch.randperm(len(sents)).type(itype)
            shuffled_sents = sents[shuffle]

            # Shuffled order.
            yield (
                torch.cat([zeros, shuffled_sents]),
                Variable(torch.FloatTensor([0]))
            )


class SentenceEncoder(nn.Module):

    def __init__(self, lstm_dim=128):
        super().__init__()
        self.lstm = nn.LSTM(300, lstm_dim)

    def forward(self, x):
        _, (hn, cn) = self.lstm(x.transpose(0, 1))
        return hn


class Model(nn.Module):

    def __init__(self, input_dim=128, lstm_dim=128):
        super().__init__()
        self.lstm = nn.LSTM(input_dim, lstm_dim)
        self.out = nn.Linear(lstm_dim, 1)

    def forward(self, x):
        _, (hn, cn) = self.lstm(x.transpose(0, 1))
        y = F.sigmoid(self.out(hn))
        return y.squeeze()


@click.command()
@click.argument('train_path', type=click.Path())
@click.argument('vectors_path', type=click.Path())
@click.argument('model_path', type=click.Path())
@click.option('--train_skim', type=int, default=10000)
@click.option('--lr', type=float, default=1e-4)
@click.option('--epochs', type=int, default=50)
@click.option('--batch_size', type=int, default=5)
@click.option('--lstm_dim', type=int, default=512)
def main(train_path, vectors_path, model_path, train_skim, lr, epochs,
    batch_size, lstm_dim):

    load_vectors(vectors_path)

    # TRAIN

    torch.manual_seed(1)
    train = Corpus(train_path, train_skim)

    sent_encoder = SentenceEncoder(lstm_dim)
    model = Model(lstm_dim, lstm_dim)

    params = list(sent_encoder.parameters()) + list(model.parameters())

    optimizer = torch.optim.Adam(params, lr=lr)

    criterion = nn.BCELoss()

    if cuda:
        sent_encoder = sent_encoder.cuda()
        model = model.cuda()
        criterion = criterion.cuda()

    train_loss = []
    for epoch in range(epochs):

        print(f'\nEpoch {epoch}')

        epoch_loss = 0
        for i, batch in enumerate(train.batches(batch_size)):

            optimizer.zero_grad()

            sents = Variable(batch.tensor()).type(ftype)
            sents = sent_encoder(sents)

            x, y = zip(*batch.xy(sents.squeeze()))

            x = torch.stack(x).type(ftype)
            y = torch.stack(y).view(-1).type(ftype)

            y_pred = model(x)

            loss = criterion(y_pred, y)
            loss.backward()

            optimizer.step()

            epoch_loss += loss.data[0]

        epoch_loss /= len(train.abstracts)
        train_loss.append(epoch_loss)
        print(epoch_loss)

        spath = os.path.join(model_path, f'sent-encoder.{epoch}.pt')
        mpath = os.path.join(model_path, f'model.{epoch}.pt')

        torch.save(sent_encoder, spath)
        torch.save(model, mpath)


if __name__ == '__main__':
    main()