import streamlit as st
import pandas as pd
import numpy as np
import re
from datetime import datetime

import plotly.express as px
import plotly.graph_objects as go

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATA_PATH = "city_events.csv"
HOST_DATA_PATH = "host_events_06-16.csv"

st.set_page_config(layout="wide", page_title="Event Dashboard", page_icon="📊")


# ---------------------------------------------------------------------------
# Cached data loaders (previously re-read from disk on every page render)
# ---------------------------------------------------------------------------
@st.cache_data
def load_events():
    df = pd.read_csv(DATA_PATH)
    df["date"] = pd.to_datetime(df["date"])
    return df


@st.cache_data
def load_host_events():
    return pd.read_csv(HOST_DATA_PATH)


# ---------------------------------------------------------------------------
# Shared helpers (factor out the repeated map / table patterns)
# ---------------------------------------------------------------------------
def clean_map_columns(df):
    df = df.copy()
    df["primary_category"] = df["primary_category"].astype(str)
    df["is_paid"] = ["unknown" if pd.isna(i) else i for i in df["is_paid"].astype(str)]
    return df


def event_map(df, title, zoom=3, height=650, extra_hover=None):
    hover_data = {
        "name": True,
        "primary_category": True,
        "date": True,
        "is_paid": True,
        "latitude": False,
        "longitude": False,
    }
    if extra_hover:
        hover_data.update(extra_hover)

    fig = px.scatter_map(
        df,
        lat="latitude",
        lon="longitude",
        title=title,
        color="primary_category",
        color_discrete_sequence=px.colors.qualitative.Light24,
        zoom=zoom,
        height=height,
        map_style="carto-darkmatter",
        hover_data=hover_data,
    )
    fig.update_traces(marker_sizemin=4, marker=dict(opacity=0.75))
    fig.update_layout(legend_title_text=title, margin=dict(l=0, r=0, t=40, b=0))
    return fig


def category_count_table(df, col="primary_category", label="Primary Category"):
    counts = df[col].value_counts().reset_index()
    counts.columns = [label, "Count"]
    counts["Percentage"] = (counts["Count"] / len(df) * 100).round(1)
    counts["Percentage"] = counts["Percentage"].map(lambda x: f"{x:g}%")
    return counts


def kpi_row(*metrics):
    """metrics: list of (label, value) tuples."""
    cols = st.columns(len(metrics))
    for col, (label, value) in zip(cols, metrics):
        col.metric(label, value)


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("📊 Event Dashboard")
st.caption("City events, hosted by organizers across the country.")

tab_overview, tab_trends, tab_hosts, tab_advanced = st.tabs(
    ["Overview", "Trends & Events", "Hosts", "Advanced: Similarity & Key Hosts"]
)

# ===========================================================================
# TAB 1 — OVERVIEW (landing page: what's in the dataset, at a glance)
# ===========================================================================
with tab_overview:
    df = load_events()

    st.subheader("Dataset Summary")
    kpi_row(
        ("Events", f"{len(df):,}"),
        ("Cities", f"{df['city'].nunique():,}"),
        ("Primary Categories", f"{df['primary_category'].nunique():,}"),
    )

    left, right = st.columns(2)

    with left:
        st.markdown("**Primary Categories by Event Count**")
        st.dataframe(category_count_table(df), use_container_width=True, hide_index=True)

    with right:
        st.markdown("**Top Tags (>5% of Events)**")
        tag_counts = (
            df["tags"].dropna().str.split(",").explode().str.strip()
            .value_counts().reset_index()
        )
        tag_counts.columns = ["Tag", "Count"]
        tag_counts["Percentage"] = tag_counts["Count"] / len(df) * 100
        tag_counts = tag_counts[tag_counts["Percentage"] > 5]
        tag_counts["Percentage"] = tag_counts["Percentage"].round(1).map(lambda x: f"{x:g}%")
        st.dataframe(tag_counts, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Event Map")

    map_df = clean_map_columns(df)

    map_mode = st.radio(
        "View", ["All categories", "By tag"], horizontal=True, label_visibility="collapsed"
    )

    if map_mode == "All categories":
        fig = event_map(map_df, "Events by Category")
        st.plotly_chart(fig, use_container_width=True)
    else:
        all_tags = sorted(df["tags"].dropna().str.split(",").explode().str.strip().unique())
        selected_tag = st.selectbox("Select a tag", all_tags)
        pattern = rf"(^|,\s*){re.escape(selected_tag)}(\s*,|$)"
        tag_df = map_df[map_df["tags"].fillna("").str.contains(pattern, regex=True)]
        fig = event_map(tag_df, f"Events Tagged '{selected_tag}' ({len(tag_df):,} events)")
        st.plotly_chart(fig, use_container_width=True)

    mapped = df.dropna(subset=["latitude", "longitude", "primary_category"])
    st.caption(f"{len(mapped):,} of {len(df):,} events have valid coordinates and are mapped.")


# ===========================================================================
# TAB 2 — TRENDS & EVENTS (merges old "Recent Events" + "Annual Overviews")
# ===========================================================================
with tab_trends:
    df_full = load_events()
    today = pd.Timestamp.today().normalize()

    view = st.radio(
        "View by", ["Rolling window", "By year"], horizontal=True
    )

    st.divider()

    # ---- Rolling window view (old "Recent Events" page) -------------------
    if view == "Rolling window":
        df = df_full[df_full["date"] <= today].copy()
        df["year_month"] = df["date"].dt.to_period("M")

        month_index = pd.period_range(end=today.to_period("M"), periods=12, freq="M")

        month_cutoff = st.selectbox(
            "Coverage window",
            options=list(range(1, 13)),
            index=11,
            format_func=lambda x: "past month" if x == 1 else "past year" if x == 12 else f"past {x} months",
        )

        cutoff = today - pd.DateOffset(months=month_cutoff)
        df = df[(df["date"] <= today) & (df["date"] >= cutoff)]

        recent_counts = df.groupby("primary_category").size().sort_values(ascending=False)
        top_n = 5
        categories = recent_counts.head(top_n).index.tolist()
        if "Other" in categories:
            categories.remove("Other")
            categories.append("Other")

        df["date"] = [i.date() for i in df["date"]]

        kpi_row(("Events", f"{len(df):,}"), ("Cities", f"{df['city'].nunique():,}"))

        def window_label(cutoff_val):
            if cutoff_val == 12:
                return "past year"
            if cutoff_val == 1:
                return "past month"
            return f"past {cutoff_val} months"

        monthly_data = {}
        for category in categories:
            cat_df = df[df["primary_category"] == category]
            monthly_counts = (
                cat_df.groupby("year_month").size()
                .reindex(month_index, fill_value=0)
                .reset_index()
            )
            monthly_counts.columns = ["year_month", "count"]
            monthly_counts["year_month"] = monthly_counts["year_month"].astype(str)
            monthly_data[category] = monthly_counts

        fig = go.Figure()
        for i, category in enumerate(categories):
            mc = monthly_data[category]
            fig.add_trace(go.Bar(x=mc["year_month"], y=mc["count"], name=category, visible=(i == 0)))

        buttons = []
        for i, category in enumerate(categories):
            visible = [False] * len(categories)
            visible[i] = True
            subtitle = f"({recent_counts[category]:,} {category} events in the {window_label(month_cutoff)})"
            buttons.append(dict(
                label=category, method="update",
                args=[{"visible": visible}, {"title": {"text": f"Event Frequency by Category<br><sup>{subtitle}</sup>"}}],
            ))

        first_category = categories[0]
        subtitle = f"({recent_counts[first_category]:,} {first_category} events in the {window_label(month_cutoff)})"

        fig.update_layout(
            title={"text": f"Event Frequency by Category<br><sup>{subtitle}</sup>"},
            updatemenus=[dict(type="buttons", buttons=buttons, direction="right",
                               showactive=True, x=0.5, xanchor="center", y=1.08, yanchor="top")],
            xaxis_title="Month", yaxis_title="Number of Events",
        )
        st.plotly_chart(fig, use_container_width=True)

        map_df = clean_map_columns(df)
        fig_map = event_map(map_df, f"Events by Category in the {window_label(month_cutoff)}", zoom=5)
        st.plotly_chart(fig_map, use_container_width=True)

        mapped = df.dropna(subset=["latitude", "longitude", "primary_category"])
        st.caption(f"{len(mapped):,} events mapped.")

    # ---- By-year view (old "Annual Overviews" page) -----------------------
    else:
        years = sorted(df_full["event_year"].dropna().unique())
        current_year = datetime.now().year
        default_index = years.index(current_year) if current_year in years else len(years) - 1

        selected_year = st.selectbox("Select year", years, index=default_index)
        df_year = df_full[df_full["event_year"] == selected_year].copy()

        kpi_row(("Events", f"{len(df_year):,}"), ("Cities", f"{df_year['city'].nunique():,}"))

        df_year["event_month"] = pd.to_datetime(df_year["date"]).dt.month_name()
        month_order = ["January","February","March","April","May","June",
                       "July","August","September","October","November","December"]

        monthly_counts = (
            df_year["event_month"].value_counts().reindex(month_order, fill_value=0).reset_index()
        )
        monthly_counts.columns = ["month", "event_count"]

        fig_months = px.bar(monthly_counts, x="month", y="event_count", text="event_count")
        fig_months.update_traces(texttemplate="%{text:,}", textposition="outside")
        fig_months.update_layout(
            title=f"Events by Month ({selected_year})", xaxis_title="Month", yaxis_title="Events"
        )
        st.plotly_chart(fig_months, use_container_width=True)

        city_counts = df_year["city"].value_counts().reset_index()
        city_counts.columns = ["city", "event_count"]
        top20 = city_counts.head(20)

        fig_cities = px.bar(
            top20.sort_values("event_count"), x="event_count", y="city",
            orientation="h", text="event_count",
        )
        fig_cities.update_traces(texttemplate="%{text:,}", textposition="outside")
        fig_cities.update_layout(
            title=f"Top 20 Cities ({selected_year})", xaxis_title="Number of Events",
            yaxis_title="City", height=700,
        )
        st.plotly_chart(fig_cities, use_container_width=True)

        map_df = clean_map_columns(df_year)
        fig_map = event_map(
            map_df, f"Events by Category ({selected_year})", zoom=3, height=700,
            extra_hover={"city": True},
        )
        st.plotly_chart(fig_map, use_container_width=True)

        mapped = df_year.dropna(subset=["latitude", "longitude", "primary_category"])
        st.caption(f"{len(mapped):,} events mapped.")


# ===========================================================================
# TAB 3 — HOSTS (old "Host Overview" page)
# ===========================================================================
with tab_hosts:
    df = load_host_events()

    kpi_row(("Hosts", f"{df['host_name'].nunique():,}"), ("Cities", f"{df['city'].nunique():,}"))

    def most_common(series):
        mode = series.mode()
        return mode.iloc[0] if not mode.empty else None

    hosts = (
        df.groupby("host_name")
        .agg(
            inferred_host_type=("inferred_host_type", most_common),
            inferred_gtm_segment=("inferred_gtm_segment", most_common),
        )
        .reset_index()
    )
    st.caption("Each host's most common inferred type and GTM segment is used below.")

    left, right = st.columns(2)
    with left:
        st.markdown("**Inferred Host Types by Host Count**")
        st.dataframe(category_count_table(hosts, "inferred_host_type", "Host Type"),
                     use_container_width=True, hide_index=True)
    with right:
        st.markdown("**Inferred GTM Segments by Host Count**")
        st.dataframe(category_count_table(hosts, "inferred_gtm_segment", "Segment"),
                     use_container_width=True, hide_index=True)


# ===========================================================================
# TAB 4 — ADVANCED: SIMILARITY & KEY HOSTS (old page, kept separate on purpose)
# ===========================================================================
with tab_advanced:
    st.info(
        "This view runs topic + location clustering to group similar nearby events "
        "and rank which hosts to reach out to first. It's more compute-intensive "
        "than the other tabs — set your filters, then click **Run grouping**.",
        icon="🔬",
    )

    from late_fusion_utils_v7 import (
        embed_descriptions,
        build_embedding_text,
        semantic_cluster,
        spatial_cluster,
        fuse_clusters,
        rescue_noise_points,
        rank_key_hosts_by_host,
        derive_recency_and_frequency,
    )

    df = load_host_events()
    df = df.dropna(subset=["latitude", "longitude", "state"]).reset_index(drop=True)

    # rank_key_hosts_by_host looks up a column literally named "event_type";
    # this dataset calls it primary_category, so mirror it under that name
    # for that one internal lookup only. User-facing labels still say "category".
    df["event_type"] = df["primary_category"]

    df = derive_recency_and_frequency(df, host_col="host_name", date_col="date")

    st.write(f"Loaded **{len(df)}** events across **{df['state'].nunique()}** states.")

    col1, col2, col3 = st.columns(3)
    with col1:
        states = sorted(df["state"].dropna().unique())
        selected_states = st.multiselect("States to include", states, default=states)
    with col2:
        categories = sorted(df["primary_category"].dropna().unique())
        selected_categories = st.multiselect("Categories to include", categories, default=categories)
    with col3:
        min_events_per_state = st.selectbox(
            "Skip states with fewer events than", [5, 10, 20, 50, 100], index=1
        )

    df = df[df["state"].isin(selected_states) & df["primary_category"].isin(selected_categories)].reset_index(drop=True)

    if df.empty:
        st.warning("No events match the selected filters.")
        st.stop()

    st.write(f"**{len(df)}** events remain after filtering.")

    with st.expander("Advanced clustering settings"):
        adv1, adv2, adv3 = st.columns(3)
        with adv1:
            semantic_min_cluster_size = st.number_input(
                "Smallest group size (by topic)", min_value=2, max_value=2000, value=5, step=1
            )
        with adv2:
            spatial_eps_meters = st.slider("How close counts as 'nearby' (meters)", 50, 5000, 500, 50)
        with adv3:
            spatial_min_samples = st.number_input(
                "Smallest group size (by location)", min_value=2, max_value=2000, value=5, step=1
            )

        rescue_noise = st.checkbox(
            "Include borderline events in their closest group", value=True,
            help="Events right on the edge between two topics get assigned to the "
                 "closest group instead of being left out, using a fixed similarity "
                 "cutoff (cosine distance 0.8) so more borderline events get pulled in.",
        )
    rescue_max_cosine_distance = 0.8

    run = st.button("Run grouping", type="primary")

    if run:
        with st.spinner("Reading event text..."):
            embedding_text = build_embedding_text(
                df, use_event_name=True, description_col="description", event_name_col="name"
            )
            embeddings = embed_descriptions(embedding_text)

        work_df = df.copy()
        work_df["semantic_cluster"] = -1
        work_df["spatial_cluster"] = -1
        work_df["fused_cluster"] = "noise"
        work_df["semantic_cluster_label"] = "noise"
        work_df["spatial_cluster_label"] = "noise"

        states_present = sorted(work_df["state"].unique())
        progress = st.progress(0.0, text="Grouping events...")
        summary_rows = []

        for i, state in enumerate(states_present):
            idx = work_df.index[work_df["state"] == state].to_numpy()
            progress.progress((i + 1) / len(states_present), text=f"Grouping events in {state}...")

            if len(idx) < min_events_per_state:
                summary_rows.append({
                    "state": state, "events": len(idx),
                    "semantic_groups": 0, "spatial_groups": 0, "final_groups": 0,
                    "status": "skipped (too few events)",
                })
                continue

            subset = work_df.loc[idx]
            emb_subset = embeddings[idx]

            sem_labels = semantic_cluster(emb_subset, min_cluster_size=semantic_min_cluster_size)
            if rescue_noise:
                sem_labels = rescue_noise_points(emb_subset, sem_labels, max_cosine_distance=rescue_max_cosine_distance)

            spa_labels = spatial_cluster(
                subset["latitude"].values, subset["longitude"].values,
                eps_meters=spatial_eps_meters, min_samples=spatial_min_samples,
            )
            fused = fuse_clusters(sem_labels, spa_labels)
            fused_with_state = [f"{state}-{f}" if f != "noise" else "noise" for f in fused]
            sem_with_state = [f"{state}-sem{s}" if s != -1 else "noise" for s in sem_labels]
            spa_with_state = [f"{state}-spa{p}" if p != -1 else "noise" for p in spa_labels]

            work_df.loc[idx, "semantic_cluster"] = sem_labels
            work_df.loc[idx, "spatial_cluster"] = spa_labels
            work_df.loc[idx, "fused_cluster"] = fused_with_state
            work_df.loc[idx, "semantic_cluster_label"] = sem_with_state
            work_df.loc[idx, "spatial_cluster_label"] = spa_with_state

            summary_rows.append({
                "state": state, "events": len(idx),
                "semantic_groups": len(set(sem_labels) - {-1}),
                "spatial_groups": len(set(spa_labels) - {-1}),
                "final_groups": len(set(fused_with_state) - {"noise"}), "status": "ok",
            })

        progress.empty()

        st.session_state["ef_result_df"] = work_df
        st.session_state["ef_summary_df"] = pd.DataFrame(summary_rows)
        st.session_state["ef_embeddings"] = embeddings

    if "ef_result_df" not in st.session_state:
        st.info("Set your options above and click **Run grouping**.")
        st.stop()

    result_df = st.session_state["ef_result_df"]
    summary_df = st.session_state["ef_summary_df"]
    embeddings = st.session_state["ef_embeddings"]

    st.subheader("Overview by state")
    st.dataframe(summary_df, use_container_width=True)

    st.subheader("Map")
    map_col1, map_col2 = st.columns(2)
    with map_col1:
        map_state = st.selectbox("View state", ["All states"] + sorted(result_df["state"].unique().tolist()))
    with map_col2:
        color_choice = st.selectbox(
            "Color by",
            ["Fused group (topic + location)", "Semantic cluster only (topic)", "Spatial cluster only (location)"],
        )
    color_col = {
        "Fused group (topic + location)": "fused_cluster",
        "Semantic cluster only (topic)": "semantic_cluster_label",
        "Spatial cluster only (location)": "spatial_cluster_label",
    }[color_choice]

    map_df = result_df if map_state == "All states" else result_df[result_df["state"] == map_state]

    fig = px.scatter_map(
        map_df, lat="latitude", lon="longitude", color=color_col,
        hover_data={
            "latitude": False, "longitude": False, "host_name": True,
            "fused_cluster": True,
            "semantic_cluster_label": color_col == "semantic_cluster_label",
            "spatial_cluster_label": color_col == "spatial_cluster_label",
            "primary_category": True,
        },
        zoom=3 if map_state == "All states" else 9, height=550, map_style="carto-darkmatter",
    )
    fig.update_layout(margin={"r": 0, "t": 0, "l": 0, "b": 0})
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        "Each dot is one event, colored by its group. 'noise' events didn't fit "
        "clearly into a group under the selected view — for the fused view that "
        "means either stage flagged it as noise; for the semantic/spatial-only "
        "views, that one stage alone did."
    )

    st.subheader("Who to contact in each group")
    st.caption(
        "One row per host (not per event); hosts with multiple events in the same "
        "group are combined. Ranked by a priority score blending **match** (how "
        "close this host's events sit to the rest of the group) and **activity** "
        "(event count in this group, weighted by recency and cadence). Recency "
        "only counts past events; frequency also factors in what's upcoming. Both "
        "are scored 0–1, higher meaning stronger."
    )

    fused_options = sorted(set(result_df["fused_cluster"]) - {"noise"})
    top_k = st.slider("Hosts to show per group", 1, 20, 5)

    display_rename = {
        "host_name": "Host",
        "events_in_this_group": "Events in this group",
        "event_types": "Categories",
        "match_score": "Match",
        "raw_influence_score": "Raw influence (events x recency x frequency)",
        "activity_score": "Activity",
        "priority_score": "Priority",
    }
    event_display_cols = [c for c in ["id", "host_name", "primary_category", "name", "description", "date"]
                           if c in result_df.columns]

    for cluster_id in fused_options:
        members = result_df[result_df["fused_cluster"] == cluster_id]
        member_idx = members.index.to_numpy()
        n_unique_hosts = members["host_name"].nunique()

        ranked_hosts = rank_key_hosts_by_host(
            result_df, embeddings, member_idx, host_id_col="host_name", top_k=top_k
        )

        with st.expander(f"{cluster_id}  —  {len(members)} events from {n_unique_hosts} hosts"):
            if ranked_hosts.empty:
                st.write("No host information available for this group.")
            else:
                st.dataframe(ranked_hosts.rename(columns=display_rename), use_container_width=True)
            st.markdown("**All events in this group:**")
            st.caption("Each row is one event. The same host may appear more than once here.")
            st.dataframe(members[event_display_cols], use_container_width=True)

    st.subheader("Full event-level data")
    st.dataframe(result_df, use_container_width=True)

    st.download_button(
        "Download full results as CSV",
        result_df.to_csv(index=False).encode("utf-8"),
        file_name="events_clustered.csv",
        mime="text/csv",
    )
