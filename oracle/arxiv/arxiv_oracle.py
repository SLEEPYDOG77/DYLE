from datasets import load_dataset
import json
import sys
import os
from os.path import join
import torch
import logging
from functools import partial
from concurrent.futures import ProcessPoolExecutor
import tempfile
import subprocess as sp
from datetime import timedelta
from time import time
from nltk.tokenize import sent_tokenize, word_tokenize
from tqdm import tqdm

from pyrouge import Rouge155
from pyrouge.utils import log
from rouge import Rouge

# Change to own Rouge Path
_ROUGE_PATH = '.../rouge/ROUGE-1.5.5/'
rouge_diff_thresh = 0
test_pruning_thresh = 0
sent_limit = 64
mode = "test"

# Evaluate ROUGE score given two directories
def eval_rouge(dec_dir, ref_dir):
    assert _ROUGE_PATH is not None
    log.get_global_console_logger().setLevel(logging.WARNING)
    dec_pattern = '(\d+).dec'
    ref_pattern = '#ID#.ref'
    cmd = '-c 95 -r 1000 -n 2 -m'
    with tempfile.TemporaryDirectory() as tmp_dir:
        Rouge155.convert_summaries_to_rouge_format(
            dec_dir, join(tmp_dir, 'dec'))
        Rouge155.convert_summaries_to_rouge_format(
            ref_dir, join(tmp_dir, 'ref'))
        Rouge155.write_config_static(
            join(tmp_dir, 'dec'), dec_pattern,
            join(tmp_dir, 'ref'), ref_pattern,
            join(tmp_dir, 'settings.xml'), system_id=1
        )
        cmd = (join(_ROUGE_PATH, 'ROUGE-1.5.5.pl')
            + ' -e {} '.format(join(_ROUGE_PATH, 'data'))
            + cmd
            + ' -a {}'.format(join(tmp_dir, 'settings.xml')))
        output = sp.check_output(cmd.split(' '), universal_newlines=True)
        R_1 = float(output.split('\n')[3].split(' ')[3])
        R_2 = float(output.split('\n')[7].split(' ')[3])
        R_L = float(output.split('\n')[11].split(' ')[3])
        print(output)

# Compute average of ROUGE score using PyROUGE (faster)
def rouge(dec, ref):
    if dec == '' or ref == '':
        return 0.0
    rouge = Rouge()
    scores = rouge.get_scores(dec, ref)
    return (scores[0]['rouge-1']['f'] + scores[0]['rouge-2']['f'] + scores[0]['rouge-l']['f']) / 3

# Get decoded sentence given index
def get_dec(text, idx):
    dec = []
    for i in idx:
        dec.append(text[i])
    return ' '.join(dec)

# Do first-round of filtering. Remove irrelevant snippets
def text_pruning(text, ref):
    new_text = []
    for i in range(len(text)):
        if not text[i] or text[i] == ".":
            continue
        try:
            cur_score = rouge(text[i], ref)
        except:
            print(text[i])
        if cur_score > test_pruning_thresh:
            new_text.append(text[i])
    return new_text

# Obtain the extractive oracle using greedy search
def get_oracle(text, ref):
    original_text = text
    text = text_pruning(text, ref)

    score = 0.0
    oracle_idx = []
    while True:
        best_score = 0.0
        best_idx = -1
        for i in range(len(text)):
            if i in oracle_idx:
                continue
            cur_idx = oracle_idx + [i]
            cur_idx.sort()
            dec = get_dec(text, cur_idx)
            cur_score = rouge(dec, ref)
            if cur_score > best_score:
                best_score = cur_score
                best_idx = i

        if best_score > score + rouge_diff_thresh:
            score = best_score
            oracle_idx += [best_idx]
            oracle_idx.sort()
        else:
            break

    original_indexes = []
    for index in oracle_idx:
        original_indexes.append(original_text.index(text[index]))

    return get_dec(text, oracle_idx), original_indexes

# Tokenize document into sentences
def process_article_sent_tokenize(article):
    article = " ".join(word_tokenize(article.lower()))
    article = sent_tokenize(article)
    return article

def insert_new(article_list, sent):
    token_list = word_tokenize(sent) 
    article_list.append(" ".join(token_list[:sent_limit]))

    if len(token_list) > sent_limit:
        insert_new(article_list, " ".join(token_list[sent_limit:]))

def process_article(article):
    article = process_article_sent_tokenize(article)
    new_article = []

    for sent in article:
        insert_new(new_article, sent)

    return new_article

# ***************************** Greedy Oracle for mode  *****************************

total_partitions = 30
dataset = load_dataset("scientific_papers", "arxiv", split=mode)
kept_indices = [idx for idx in range(len(dataset)) if not os.path.isfile('./index_{}/{}.dec'.format(mode, idx))]
num_workers = 24
per_batch = len(kept_indices) // num_workers

def extract_oracle(samples):
    for idx, data_entity in samples:

        if os.path.isfile('./index_{}/{}.dec'.format(mode, idx)):
            continue

        article = data_entity["article"]
        abstract = data_entity["abstract"]

        process_article(article)
        abstract = " ".join(word_tokenize(abstract.lower()))

        oracle, indexes = get_oracle(article, abstract)

        # Obtain oracle index
        with open('./index_{}/{}.dec'.format(mode, idx), 'w') as f:
            print(indexes, file=f)

        # Obtain gold summary reference
        with open('./ref_{}/{}.ref'.format(mode, idx), 'w') as f:
            for sent in sent_tokenize(abstract):
                print(sent, file=f)

        # Obtain decoded oracle
        with open('./dec_{}/{}.dec'.format(mode, idx), 'w') as f:
            for sent in sent_tokenize(oracle):
                print(sent, file=f)

# Process oracle in parallel
with ProcessPoolExecutor(max_workers=num_workers) as executor:
    futures = []
    samples = []
    for idx, sample in enumerate(dataset):
        if idx in kept_indices:
            samples.append((idx, sample))
        else:
            continue
        if len(samples) % per_batch == 0 or idx == len(dataset) - 1:
            futures.append(executor.submit(partial(extract_oracle, samples)))
            samples = []

    results = [future.result() for future in tqdm(futures)]

print('Start evaluating ROUGE score')
eval_rouge('./dec_{}/'.format(mode), './ref_{}'.format(mode))