"""Rank a user's chunks against a search query using BM25 keyword scoring.

BM25 is the classic search-engine formula: a chunk scores higher when it
contains the query's words often, and rare words count more than common ones.
"""
import math
import re
from collections import Counter

# Words so common they say nothing about the topic.
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "did", "do",
    "does", "for", "from", "had", "has", "have", "he", "her", "his", "how", "i",
    "if", "in", "into", "is", "it", "its", "me", "my", "of", "on", "or", "our",
    "she", "so", "than", "that", "the", "their", "them", "then", "there",
    "these", "they", "this", "to", "was", "we", "were", "what", "when", "where",
    "which", "who", "whom", "why", "will", "with", "would", "you", "your",
    "about", "tell", "give", "please", "document", "documents",
}

# BM25's two tuning knobs, at their standard textbook values.
K1 = 1.5   # how quickly repeated words stop adding score
B = 0.75   # how much long chunks are penalised


def tokenize(text):
    """'The CTC was 12 LPA.' -> ['ctc', '12', 'lpa']"""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if w not in STOP_WORDS and len(w) > 1]


def pick_terms(query, limit):
    """The first `limit` distinct search words of `query`. The user's own words
    come first in the query, so they are always kept; extra keywords fill the rest.
    Short words (1-2 letters) are only used if nothing else is left, because a
    substring match on them hits almost every passage.
    Words contain only a-z and 0-9, so they are safe to put inside a ZCQL query."""
    terms = list(dict.fromkeys(tokenize(query)))
    long_terms = [t for t in terms if len(t) >= 3]
    return (long_terms or terms)[:limit]


def rank_chunks(query, chunks, top_k=5):
    """Return the `top_k` best chunks for `query`, best first.

    `chunks` is a list of dicts that each have a "content" key.
    Chunks that share no words with the query are dropped.
    """
    query_terms = set(tokenize(query))
    if not query_terms or not chunks:
        return []

    docs = [Counter(tokenize(c["content"])) for c in chunks]
    avg_len = sum(sum(d.values()) for d in docs) / len(docs) or 1

    # Inverse document frequency: rarer words get a bigger weight.
    idf = {}
    for term in query_terms:
        containing = sum(1 for d in docs if term in d)
        idf[term] = math.log(1 + (len(docs) - containing + 0.5) / (containing + 0.5))

    scored = []
    for chunk, counts in zip(chunks, docs):
        length = sum(counts.values())
        score = 0.0
        for term in query_terms:
            freq = counts.get(term, 0)
            if freq:
                score += idf[term] * freq * (K1 + 1) / (freq + K1 * (1 - B + B * length / avg_len))
        if score > 0:
            scored.append((score, chunk))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [chunk for _, chunk in scored[:top_k]]
