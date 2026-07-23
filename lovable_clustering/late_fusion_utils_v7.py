"""
Late-fusion / two-step partitioning clustering utilities.

Stage 1 (semantic): SBERT embeddings on event text -> HDBSCAN on
                     cosine distance -> semantic_cluster label per event.
                     Optionally "rescues" borderline noise points into
                     their nearest group (see rescue_noise_points).
Stage 2 (spatial):  DBSCAN with a haversine metric directly on lat/lon
                     (real eps in meters) -> spatial_cluster label per event.
Fusion:              final group = intersection of (semantic_cluster,
                     spatial_cluster). An event that is noise in EITHER
                     stage is noise in the final result.

Host ranking:        influence_score() and rank_key_hosts() combine how
                     well a host's events fit a group with how active
                     that host generally is, then rank_key_hosts_by_host()
                     collapses this to one row per host for outreach lists.

pip install sentence-transformers scikit-learn hdbscan numpy pandas
"""

import datetime

import numpy as np
import pandas as pd
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_distances
from sklearn.cluster import DBSCAN
import hdbscan

EARTH_RADIUS_M = 6_371_000

# Default cap for rescuing borderline/noise events, used when the
# dashboard's rescue-distance slider isn't overriding it. Cosine distance
# ranges 0-2; same-topic SBERT text is typically well under 0.3, unrelated
# text is usually well above 0.6. The dashboard exposes a slider from 0.30
# (strict, same-topic only) up to 0.70 (loose, pulls in more borderline
# events at the risk of grouping less-similar ones together).
RESCUE_MAX_COSINE_DISTANCE = 0.30


# ---------------------------------------------------------------------------
# SBERT embeddings
# ---------------------------------------------------------------------------
_MODEL_CACHE = {}


def get_sbert_model(model_name="all-MiniLM-L6-v2"):
    if model_name not in _MODEL_CACHE:
        _MODEL_CACHE[model_name] = SentenceTransformer(model_name)
    return _MODEL_CACHE[model_name]


def embed_descriptions(descriptions, model_name="all-MiniLM-L6-v2"):
    model = get_sbert_model(model_name)
    return model.encode(
        descriptions.fillna("").tolist(),
        normalize_embeddings=True,
        show_progress_bar=False,
    )


def build_embedding_text(df, use_event_name=True, description_col="event_description",
                          event_name_col="event_name"):
    """
    Combines event_name + event_description into one text field for
    embedding, when both are available and use_event_name=True.
    """
    if use_event_name and event_name_col in df.columns:
        return (
            df[event_name_col].fillna("").astype(str)
            + ". "
            + df[description_col].fillna("").astype(str)
        )
    return df[description_col].fillna("").astype(str)


def find_cluster_medoids(embeddings, indices, top_k=3):
    """
    Kept for backward compatibility / ad-hoc inspection. For the
    marketing-facing "who should we contact" tables, prefer
    rank_key_hosts_by_host below, which groups by host and factors in
    host activity, not just semantic centrality of one event.
    """
    if len(indices) == 0:
        return []
    sub_emb = embeddings[indices]
    D = cosine_distances(sub_emb)
    avg_dist = D.mean(axis=1)
    order = np.argsort(avg_dist)
    top = order[: min(top_k, len(indices))]
    return [indices[i] for i in top]


# ---------------------------------------------------------------------------
# Noise rescue (fixed, non-adjustable distance cap)
# ---------------------------------------------------------------------------
def rescue_noise_points(embeddings_subset, labels, max_cosine_distance=RESCUE_MAX_COSINE_DISTANCE):
    """
    Reassigns events HDBSCAN labeled as noise (-1) to their nearest group,
    but ONLY if that group is within max_cosine_distance — otherwise the
    event stays noise. This avoids forcing a genuinely unusual event into
    a group it doesn't actually resemble, while still rescuing the many
    borderline cases that a density-based algorithm tends to over-flag.
    """
    labels = np.asarray(labels).copy()
    cluster_ids = [c for c in set(labels) if c != -1]
    if not cluster_ids:
        return labels

    centroids = []
    for c in cluster_ids:
        member_emb = embeddings_subset[labels == c]
        centroid = member_emb.mean(axis=0)
        norm = np.linalg.norm(centroid)
        centroids.append(centroid / norm if norm > 0 else centroid)
    centroid_matrix = np.vstack(centroids)

    noise_idx = np.where(labels == -1)[0]
    if len(noise_idx) == 0:
        return labels

    dists = cosine_distances(embeddings_subset[noise_idx], centroid_matrix)
    nearest_cluster_pos = dists.argmin(axis=1)
    nearest_dist = dists.min(axis=1)

    for i, row_pos in enumerate(noise_idx):
        if nearest_dist[i] <= max_cosine_distance:
            labels[row_pos] = cluster_ids[nearest_cluster_pos[i]]

    return labels


# ---------------------------------------------------------------------------
# Host influence scoring
# ---------------------------------------------------------------------------
def influence_score(event_count, last_event_date, frequency_label, reference_date=None):
    """
    influence_score = event_count x recency_weight x frequency_weight

    - event_count      : number of events hosted (pass the count WITHIN
                          the group being ranked, not a global lifetime
                          count, if you want "influence within the cluster")
    - last_event_date  : date of most recent event -> recency_weight
    - frequency_label   : daily/weekly/biweekly/monthly/occasional/one-off
                          -> frequency_weight

    This is a RAW, unbounded score (not scaled to [0,1]) — it's meant to
    rank hosts relative to each other within the same group, not to be
    read as an absolute probability. Note the multiplicative form means
    a missing recency_weight (no last_event data) or an unrecognized
    frequency zeroes out the whole score for every host equally, which
    just means ranking falls back to whatever other signal it's
    combined with (e.g. match_score) — see rank_key_hosts_by_host.
    """
    if reference_date is None:
        reference_date = datetime.date.today()

    if last_event_date is None:
        recency_weight = 0.0
    else:
        if hasattr(last_event_date, "date"):
            last_event_date = last_event_date.date()
        days_ago = (reference_date - last_event_date).days
        if days_ago <= 90:
            recency_weight = 1.0
        elif days_ago <= 365:
            recency_weight = 0.5
        else:
            recency_weight = 0.1

    freq_scores = {
        "daily": 1.0, "weekly": 0.9, "biweekly": 0.7,
        "monthly": 0.5, "occasional": 0.3, "one-off": 0.1,
    }
    freq = freq_scores.get(str(frequency_label).lower(), 0.2)

    return round(event_count * recency_weight * freq, 4)


def rank_key_hosts(df_cluster, embeddings_all, cluster_row_indices,
                    event_count_col="event_count",
                    last_event_col="last_event",
                    frequency_col="frequency",
                    top_k=3):
    """
    Rank INDIVIDUAL EVENT ROWS within a cluster by a combined score:
      combined_score = 0.5 * representativeness + 0.5 * influence

    representativeness: how close this event's embedding is to the
                         cluster centroid (1 - normalized avg cosine
                         distance to other members).
    influence:           host-level volume / recency / cadence via
                         influence_score().

    NOTE: cluster_row_indices must be label-based (i.e. values from
    df_cluster.index), and this uses .loc accordingly so it works whether
    df_cluster is the full DataFrame or an already-filtered subset.

    Returns event-level rows (a host with 3 events in this cluster can
    appear up to 3 times). For a deduplicated, one-row-per-host outreach
    list, use rank_key_hosts_by_host instead.
    """
    cluster_row_indices = list(cluster_row_indices)
    if len(cluster_row_indices) == 0:
        return []

    sub_emb = embeddings_all[cluster_row_indices]
    D = cosine_distances(sub_emb)
    avg_dist = D.mean(axis=1)
    max_d = avg_dist.max() if avg_dist.max() > 0 else 1.0
    repr_scores = 1.0 - (avg_dist / max_d)

    today = datetime.date.today()
    inf_scores = []
    for row_idx in cluster_row_indices:
        row = df_cluster.loc[row_idx]
        ec = int(row[event_count_col]) if event_count_col in row.index and pd.notna(row[event_count_col]) else 1
        le = row.get(last_event_col, None)
        if le is not None and not (isinstance(le, float) and pd.isna(le)):
            le = pd.to_datetime(le, errors="coerce")
            le = le.date() if le is not pd.NaT else None
        else:
            le = None
        freq = row[frequency_col] if frequency_col in row.index and pd.notna(row.get(frequency_col)) else "occasional"
        inf_scores.append(influence_score(ec, le, freq, reference_date=today))

    inf_scores = np.array(inf_scores)
    combined = 0.5 * repr_scores + 0.5 * inf_scores

    order = np.argsort(-combined)
    top = order[: min(top_k, len(order))]
    return [
        (cluster_row_indices[i], round(float(combined[i]), 4),
         round(float(repr_scores[i]), 4), round(float(inf_scores[i]), 4))
        for i in top
    ]


def rank_key_hosts_by_host(df, embeddings, cluster_row_indices, host_id_col="host_id",
                            last_event_col="last_event", frequency_col="frequency",
                            top_k=5):
    """
    Host-level ranking within one group, combining:
      - match_score    : 1 - (this host's most central event's avg cosine
                         distance to every OTHER event in the group),
                         rescaled 0-1 within the group. This is the true
                         "avg distance to the rest of the cluster" measure
                         (not a centroid-similarity shortcut).
      - activity_score : influence_score() = event_count x recency_weight
                         x frequency_weight, where event_count is this
                         host's number of events WITHIN THIS GROUP (not a
                         global lifetime count) — i.e. influence measured
                         within the cluster, as requested. The raw product
                         is then rescaled 0-1 within the group so it can
                         be fairly blended with match_score.

    priority_score = 0.5 * match_score + 0.5 * activity_score

    Returns one row per host (event_description and other long free-text
    fields intentionally excluded — this is an outreach list, not a log).
    """
    cluster_row_indices = np.asarray(list(cluster_row_indices))
    if len(cluster_row_indices) == 0 or host_id_col not in df.columns:
        return pd.DataFrame()

    cluster_emb = embeddings[cluster_row_indices]
    D = cosine_distances(cluster_emb)          # true pairwise distances within this group
    avg_dist = D.mean(axis=1)                    # each event's avg distance to every other member
    max_d = avg_dist.max() if avg_dist.max() > 0 else 1.0
    event_repr_scores = 1.0 - (avg_dist / max_d)  # 0-1, higher = more central to the group

    cluster_df = df.loc[cluster_row_indices].copy()
    cluster_df["_event_repr_score"] = event_repr_scores

    has_last_event = last_event_col in df.columns
    has_frequency = frequency_col in df.columns

    today = datetime.date.today()
    raw = []
    for host_id, group in cluster_df.groupby(host_id_col):
        # host's match_score = their single most representative event
        best_match = float(group["_event_repr_score"].max())
        event_count_in_group = len(group)  # influence measured WITHIN this cluster

        last_event = None
        if has_last_event:
            parsed = pd.to_datetime(group[last_event_col], errors="coerce").max()
            if pd.notna(parsed):
                last_event = parsed.date() if hasattr(parsed, "date") else parsed

        frequency = "occasional"
        if has_frequency:
            modes = group[frequency_col].dropna().mode()
            if not modes.empty:
                frequency = modes.iloc[0]

        raw_influence = influence_score(event_count_in_group, last_event, frequency, reference_date=today)

        raw.append({
            "host_id": host_id,
            "group": group,
            "match_score": best_match,
            "events_in_this_group": event_count_in_group,
            "raw_influence_score": raw_influence,
        })

    # Rescale the raw multiplicative influence score to 0-1 WITHIN this
    # group so it blends fairly with match_score. If every host has the
    # same raw score (e.g. no recency/frequency data, so it's 0 for
    # everyone), everyone gets 1.0 here -- ranking then falls back
    # entirely to match_score, rather than incorrectly zeroing everyone out.
    raw_influences = np.array([r["raw_influence_score"] for r in raw], dtype=float)
    if raw_influences.max() > raw_influences.min():
        norm_influences = (raw_influences - raw_influences.min()) / (raw_influences.max() - raw_influences.min())
    else:
        norm_influences = np.ones_like(raw_influences)

    rows = []
    for r, activity_score in zip(raw, norm_influences):
        group = r["group"]
        priority_score = 0.5 * r["match_score"] + 0.5 * float(activity_score)
        row = {
            host_id_col: r["host_id"],
            "events_in_this_group": r["events_in_this_group"],
            "event_types": ", ".join(sorted(group["event_type"].dropna().unique())) if "event_type" in group.columns else "",
            "match_score": round(r["match_score"], 3),
            "raw_influence_score": round(r["raw_influence_score"], 3),
            "activity_score": round(float(activity_score), 3),
            "priority_score": round(priority_score, 3),
        }
        for c in ["host_name", "contact_email", "contact_phone"]:
            if c in group.columns:
                row[c] = group[c].iloc[0]
        rows.append(row)

    result = pd.DataFrame(rows).sort_values("priority_score", ascending=False).reset_index(drop=True)
    if top_k is not None:
        result = result.head(top_k)
    return result


# ---------------------------------------------------------------------------
# Stage 1: semantic clustering (HDBSCAN on cosine distance)
# ---------------------------------------------------------------------------
def semantic_cluster(embeddings_subset, min_cluster_size=5, min_samples=None):
    n = embeddings_subset.shape[0]
    if n < min_cluster_size:
        return np.full(n, -1)
    D_text = cosine_distances(embeddings_subset).astype(np.float64)
    clusterer = hdbscan.HDBSCAN(
        metric="precomputed",
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
    )
    return clusterer.fit_predict(D_text)


# ---------------------------------------------------------------------------
# Stage 2: spatial clustering (real DBSCAN, haversine metric, eps in meters)
# ---------------------------------------------------------------------------
def spatial_cluster(lat, lon, eps_meters=300, min_samples=5):
    lat = np.asarray(lat, dtype=float).reshape(-1)
    lon = np.asarray(lon, dtype=float).reshape(-1)
    n = len(lat)
    if n < min_samples:
        return np.full(n, -1)

    coords_rad = np.radians(np.column_stack([lat, lon]))
    eps_rad = eps_meters / EARTH_RADIUS_M

    db = DBSCAN(
        eps=eps_rad,
        min_samples=min_samples,
        metric="haversine",
        algorithm="ball_tree",
    )
    return db.fit_predict(coords_rad)


# ---------------------------------------------------------------------------
# Fusion: intersect the two partitions
# ---------------------------------------------------------------------------
def fuse_clusters(semantic_labels, spatial_labels):
    """
    A host event is 'noise' in the fused result if it was noise (-1) in
    either stage — hard partition intersection, not a soft tradeoff.
    """
    n = len(semantic_labels)
    fused = np.empty(n, dtype=object)
    for i in range(n):
        s, p = semantic_labels[i], spatial_labels[i]
        if s == -1 or p == -1:
            fused[i] = "noise"
        else:
            fused[i] = f"sem{s}_spa{p}"
    return fused


# ---------------------------------------------------------------------------
# Derive last_event / frequency from a real event-date column, for
# datasets (like host_events CSVs with a `date` column) that don't come
# with pre-labeled recency/cadence fields the way the synthetic sample
# data does. rank_key_hosts_by_host() looks for columns named exactly
# "last_event" and "frequency" -- this produces both from real dates.
# ---------------------------------------------------------------------------
def derive_recency_and_frequency(df, host_col, date_col, reference_date=None):
    """
    For each host, computes:
      - last_event : their most recent event date, considering ONLY
                     events on or before reference_date (default: today).
                     Future-dated events are excluded from this — a host
                     whose only events are still upcoming gets no
                     last_event (None), not an inflated "very recent" score.
      - frequency  : a coarse cadence label (daily/weekly/biweekly/
                     monthly/occasional/one-off) inferred from the median
                     gap between ALL of a host's events — past AND
                     future. Upcoming events are a reasonable signal of
                     how often a host typically runs things, even before
                     they've happened, so they're included here (unlike
                     last_event, which only reflects what's actually
                     already occurred). Hosts with only one event total
                     get 'one-off'.

    Future events are never dropped from the returned data — they still
    appear for clustering and event listings either way.

    Returns a COPY of df with 'last_event' and 'frequency' columns added
    (same value repeated across all of a host's rows; NaN/NaT for
    last_event on hosts with no past events).
    """
    import datetime

    if reference_date is None:
        reference_date = datetime.date.today()
    reference_date = pd.Timestamp(reference_date)

    work = df.copy()
    work[date_col] = pd.to_datetime(work[date_col], errors="coerce")

    # Recency: past events only.
    past = work[work[date_col] <= reference_date]
    host_last = past.groupby(host_col)[date_col].max().rename("last_event")

    # Cadence: ALL events, past and future — an upcoming event still
    # tells you something about how often this host tends to host.
    def _cadence(dates):
        dates = dates.dropna().sort_values()
        if len(dates) <= 1:
            return "one-off"
        gaps = dates.diff().dropna().dt.days
        if gaps.empty:
            return "one-off"
        median_gap = gaps.median()
        if median_gap <= 2:
            return "daily"
        elif median_gap <= 10:
            return "weekly"
        elif median_gap <= 20:
            return "biweekly"
        elif median_gap <= 45:
            return "monthly"
        else:
            return "occasional"

    host_freq = work.groupby(host_col)[date_col].apply(_cadence).rename("frequency")

    # Hosts with zero PAST events won't appear in `past` at all, so the
    # left-merge below naturally leaves them with NaN last_event --
    # correctly signaling "no history", which influence_score() treats
    # as recency_weight = 0.0. Their frequency, however, is still
    # computed normally from `work` (all events) above.
    host_info = pd.concat([host_last, host_freq], axis=1).reset_index()
    return work.merge(host_info, on=host_col, how="left")
