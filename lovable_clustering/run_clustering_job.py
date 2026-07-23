# run_clustering_job.py
import os
import pandas as pd
from supabase import create_client
from late_fusion_utils_v7 import (
    embed_descriptions, build_embedding_text, semantic_cluster,
    spatial_cluster, fuse_clusters, rescue_noise_points,
    rank_key_hosts_by_host, derive_recency_and_frequency,
)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]  # never expose to Lovable
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

def load_events():
    resp = supabase.table("events_host1").select("*").execute()
    return pd.DataFrame(resp.data)

def run():
    df = load_events().dropna(subset=["latitude", "longitude", "state"]).reset_index(drop=True)
    df["event_type"] = df["primary_category"]
    df = derive_recency_and_frequency(df, host_col="host_name", date_col="date")

    run_row = supabase.table("cluster_runs").insert({
        "params": {"semantic_min_cluster_size": 5, "spatial_eps_meters": 500, "spatial_min_samples": 5},
    }).execute().data[0]
    run_id = run_row["id"]

    embedding_text = build_embedding_text(df, description_col="description", event_name_col="name")
    embeddings = embed_descriptions(embedding_text)

    cluster_rows, host_rows = [], []

    for state, subset in df.groupby("state"):
        idx = subset.index.to_numpy()
        if len(idx) < 10:
            continue

        emb_subset = embeddings[idx]
        sem_labels = rescue_noise_points(emb_subset, semantic_cluster(emb_subset, min_cluster_size=5))
        spa_labels = spatial_cluster(subset["latitude"].values, subset["longitude"].values,
                                      eps_meters=500, min_samples=5)
        fused = fuse_clusters(sem_labels, spa_labels)
        fused_with_state = [f"{state}-{f}" if f != "noise" else "noise" for f in fused]

        subset = subset.copy()
        subset["fused_cluster"] = fused_with_state
        subset["semantic_cluster_label"] = [f"{state}-sem{s}" if s != -1 else "noise" for s in sem_labels]
        subset["spatial_cluster_label"] = [f"{state}-spa{p}" if p != -1 else "noise" for p in spa_labels]

        for _, row in subset.iterrows():
            cluster_rows.append({
                "event_id": row["id"], "cluster_run_id": run_id, "state": state,
                "semantic_cluster_label": row["semantic_cluster_label"],
                "spatial_cluster_label": row["spatial_cluster_label"],
                "fused_cluster": row["fused_cluster"],
            })

        for cluster_id in sorted(set(fused_with_state) - {"noise"}):
            member_idx = subset[subset["fused_cluster"] == cluster_id].index.to_numpy()
            ranked = rank_key_hosts_by_host(df, embeddings, member_idx, host_id_col="host_name", top_k=20)
            for _, r in ranked.iterrows():
                host_rows.append({
                    "cluster_run_id": run_id, "fused_cluster": cluster_id,
                    "host_name": r["host_name"], "events_in_this_group": int(r["events_in_this_group"]),
                    "event_types": r["event_types"], "match_score": float(r["match_score"]),
                    "raw_influence_score": float(r["raw_influence_score"]),
                    "activity_score": float(r["activity_score"]), "priority_score": float(r["priority_score"]),
                })

    if cluster_rows:
        supabase.table("event_clusters").insert(cluster_rows).execute()
    if host_rows:
        supabase.table("host_rankings").insert(host_rows).execute()

    print(f"Run {run_id}: {len(cluster_rows)} events clustered, {len(host_rows)} host rankings written.")

if __name__ == "__main__":
    run()