

import numpy as np

import os
import click
import torch
import attr
import random
import ujson

from tqdm import tqdm
from itertools import islice
from glob import glob
from boltons.iterutils import pairwise, chunked_iter
from scipy import stats

from torch import nn
from torch.nn.utils.rnn import pack_padded_sequence
from torch.autograd import Variable
from torch.nn import functional as F

from sorder.cuda import CUDA, ftype, itype
from sorder.vectors import LazyVectors
from sorder.utils import checkpoint, pad_and_pack


vectors = LazyVectors.read()


def read_abstracts(path, maxlen):
    """Parse abstract JSON lines.
    """
    for path in glob(os.path.join(path, '*.json')):
        with open(path) as fh:
            for line in fh:

                # Parse JSON.
                abstract = Abstract.from_line(line)

                # Filter by length.
                if len(abstract.sentences) < maxlen:
                    yield abstract


@attr.s
class Sentence:

    tokens = attr.ib()

    def tensor(self, dim=300):
        """Stack word vectors.
        """
        x = [
            vectors[t] if t in vectors else np.zeros(dim)
            for t in self.tokens
        ]

        x = np.array(x)
        x = torch.from_numpy(x)
        x = x.float()

        return x


@attr.s
class Abstract:

    sentences = attr.ib()

    @classmethod
    def from_line(cls, line):
        """Parse JSON, take tokens.
        """
        json = ujson.loads(line.strip())

        return cls([
            Sentence(s['token'])
            for s in json['sentences']
        ])


@attr.s
class Batch:

    abstracts = attr.ib()

    def packed_sentence_tensor(self, size=50):
        """Pack sentence tensors.
        """
        sents = [
            Variable(s.tensor()).type(ftype)
            for a in self.abstracts
            for s in a.sentences
        ]

        return pad_and_pack(sents, size)

    def unpack_sentences(self, encoded):
        """Unpack encoded sentences.
        """
        start = 0
        for ab in self.abstracts:
            end = start + len(ab.sentences)
            yield encoded[start:end]
            start = end


class Corpus:

    def __init__(self, path, skim=None, maxlen=10):
        """Load abstracts into memory.
        """
        reader = read_abstracts(path, maxlen)

        if skim:
            reader = islice(reader, skim)

        self.abstracts = list(tqdm(reader, total=skim))

    def random_batch(self, size):
        """Query random batch.
        """
        return Batch(random.sample(self.abstracts, size))


class Encoder(nn.Module):

    def __init__(self, input_dim, lstm_dim):
        super().__init__()
        self.lstm = nn.LSTM(input_dim, lstm_dim, batch_first=True,
            bidirectional=True)

    def forward(self, x):
        _, (hn, cn) = self.lstm(x)
        # Cat forward + backward hidden layers.
        return hn.transpose(0, 1).contiguous().view(hn.data.shape[1], -1)


class Classifier(nn.Module):

    def __init__(self, input_dim, lin_dim):
        super().__init__()
        self.lin1 = nn.Linear(input_dim, lin_dim)
        self.lin2 = nn.Linear(lin_dim, lin_dim)
        self.lin3 = nn.Linear(lin_dim, lin_dim)
        self.lin4 = nn.Linear(lin_dim, lin_dim)
        self.lin5 = nn.Linear(lin_dim, lin_dim)
        self.out = nn.Linear(lin_dim, 1)

    def forward(self, x):
        y = F.relu(self.lin1(x))
        y = F.relu(self.lin2(y))
        y = F.relu(self.lin3(y))
        y = F.relu(self.lin4(y))
        y = F.relu(self.lin5(y))
        y = F.sigmoid(self.out(y))
        return y.squeeze()


def train_batch(batch, sentence_encoder, window_encoder, classifier):
    """Train the batch.
    """
    x, reorder = batch.packed_sentence_tensor()

    # Encode sentences.
    sents = sentence_encoder(x)[reorder]

    # Generate positive / negative examples.
    examples = []
    for ab in batch.unpack_sentences(sents):
        for i in range(len(ab)-2):

            window = ab[i:]

            # Shuffle context.
            perm = torch.randperm(len(window)).type(itype)
            shuffled_window = window[perm]

            first = window[0]
            other = random.choice(window[1:])

            # First / not-first.
            examples.append((shuffled_window, first, 1))
            examples.append((shuffled_window, other, 0))

    windows, sentences, ys = zip(*examples)

    # Pad / pack windows.
    windows, reorder = pad_and_pack(windows, 10)

    # Encode windows.
    windows = window_encoder(windows)[reorder]

    # Stack (context, sentence).
    x = torch.stack([
        torch.cat([w, s])
        for w, s in zip(windows, sentences)
    ])

    y = Variable(torch.FloatTensor(ys)).type(ftype)

    return y, classifier(x)


def train(train_path, model_path, train_skim, lr, epochs, epoch_size,
    batch_size, lstm_dim, lin_dim):
    """Train model.
    """
    train = Corpus(train_path, train_skim)

    sent_encoder = Encoder(300, lstm_dim)
    window_encoder = Encoder(2*lstm_dim, lstm_dim)
    classifier = Classifier(4*lstm_dim, lin_dim)

    params = (
        list(sent_encoder.parameters()) +
        list(window_encoder.parameters()) +
        list(classifier.parameters())
    )

    optimizer = torch.optim.Adam(params, lr=lr)

    loss_func = nn.BCELoss()

    if CUDA:
        sent_encoder = sent_encoder.cuda()
        window_encoder = window_encoder.cuda()
        classifier = classifier.cuda()

    for epoch in range(epochs):

        print(f'\nEpoch {epoch}')

        epoch_loss = 0
        epoch_correct = 0
        epoch_total = 0
        for _ in tqdm(range(epoch_size)):

            optimizer.zero_grad()

            batch = train.random_batch(batch_size)

            y, y_pred = train_batch(batch, sent_encoder, \
                window_encoder, classifier)

            loss = loss_func(y_pred, y)
            loss.backward()

            optimizer.step()

            epoch_loss += loss.data[0]
            epoch_correct += (y_pred.round() == y).sum().data[0]
            epoch_total += len(y)

        # checkpoint(model_path, 'm1', m1, epoch)
        # checkpoint(model_path, 'm2', m2, epoch)

        print(epoch_loss / epoch_size)
        print(epoch_correct / epoch_total)


# class PickFirst:

    # def __init__(self, sents, model):

        # self.sents = sents
        # self.model = model

        # self.order = torch.LongTensor(range(len(sents)))

    # def swap(self, i1, i2):
        # """Given a context, predict first sentence, swap into place.
        # """
        # context = self.sents[self.order][i1:i2]

        # context_mean = context.mean(0)

        # x = torch.stack([
            # torch.cat([context_mean, sent])
            # for sent in context
        # ])

        # pred = self.model(x)
        # midx = i1 + np.argmax(pred.data.tolist())

        # # Swap max into first slot.
        # self.order[i1], self.order[midx] = self.order[midx], self.order[i1]

    # def predict(self):
        # """Pick first for all right-side contexts.
        # """
        # for i in range(len(self.sents)):
            # self.swap(i, len(self.sents)+1)

        # return self.order.tolist()


# def predict(test_path, m1_path, m2_path, test_skim, map_source, map_target):
    # """Predict order.
    # """
    # test = Corpus(test_path, test_skim)

    # m1 = torch.load(m1_path, map_location={map_source: map_target})
    # m2 = torch.load(m2_path, map_location={map_source: map_target})

    # kts = []
    # correct = 0
    # for batch in tqdm(test.batches(100)):

        # batch.shuffle()

        # encoded = m1.encode_batch(batch)

        # for ab, sents in zip(batch.abstracts, encoded):

            # gold = np.argsort([s.position for s in ab.sentences])
            # pred = PickFirst(sents, m2).predict()

            # kt, _ = stats.kendalltau(gold, pred)
            # kts.append(kt)

            # if kt == 1:
                # correct += 1

    # print(sum(kts) / len(kts))
    # print(correct / len(kts))
