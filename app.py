"""
Real Estate Auction Pipeline
=============================
A Streamlit executive dashboard for tracking, underwriting, and managing
real estate auction deals.

Features
--------
- Deal intake form (address, county, auction date, opening bid, max bid,
  estimated ARV, estimated repair costs) with validation.
- Underwriting engine: MAO = ARV * 0.70 - Repairs, Net Profit, Projected
  ROI %, and a color-coded Deal Quality Badge (Strong Buy / Marginal /
  Overpriced / Pass).
- SQLite persistence (auctions.db) with per-deal delete.
- Interactive, filterable/sortable deal board with KPI metric cards and
  one-click CSV export of the filtered board.
- Underwriting Guide tab explaining the math in plain English.

Run with:  streamlit run app.py --server.port 8501 --server.headless true
"""

import sqlite3
import time
from datetime import date

import pandas as pd
import streamlit as st

DB_PATH = "auctions.db"

# ---------------------------------------------------------------------------
# Page config + executive dark-mode styling
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Real Estate Auction Pipeline",
    page_icon="\U0001f3db\ufe0f",
    layout="wide",
    initial_sidebar_state="expanded",
)

CUSTOM_CSS = """
<style>
  /* App background and text */
  .stApp {
    background-color: #0e1117;
    color: #e6e9ef;
  }
  /* Headings */
  h1, h2, h3 {
    color: #f2f4f8 !important;
    font-weight: 700;
  }
  /* Sidebar */
  section[data-testid="stSidebar"] {
    background-color: #14181f;
    border-right: 1px solid #262c38;
  }
  section[data-testid="stSidebar"] * {
    color: #d7dce5 !important;
  }
  /* Inputs */
  .stTextInput input, .stNumberInput input, .stDateInput input,
  .stSelectbox div[data-baseweb="select"] > div {
    background-color: #1a1f2a !important;
    color: #e6e9ef !important;
    border: 1px solid #2e3542 !important;
    border-radius: 10px !important;
  }
  /* Buttons */
  .stButton > button, .stDownloadButton > button {
    background-color: #2563eb !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 10px !important;
    font-weight: 600;
    padding: 0.45rem 1.1rem;
  }
  .stButton > button:hover, .stDownloadButton > button:hover {
    background-color: #1d4ed8 !important;
  }
  /* Danger (delete) buttons */
  div[data-testid="stVerticalBlock"] .stButton > button[kind="secondary"] {
    background-color: #7f1d1d !important;
  }
  /* Tabs */
  .stTabs [data-baseweb="tab-list"] {
    gap: 8px;
  }
  .stTabs [data-baseweb="tab"] {
    background-color: #1a1f2a;
    border: 1px solid #2e3542;
    border-radius: 10px 10px 0 0;
    color: #c8cfdb;
    padding: 8px 18px;
    font-weight: 600;
  }
  .stTabs [aria-selected="true"] {
    background-color: #2563eb !important;
    color: #ffffff !important;
    border-color: #2563eb !important;
  }
  /* Metric cards */
  div[data-testid="stMetric"] {
    background-color: #161b24;
    border: 1px solid #2a3140;
    border-radius: 14px;
    padding: 16px 18px;
  }
  div[data-testid="stMetric"] label {
    color: #9aa4b5 !important;
    font-weight: 600;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    font-size: 0.78rem !important;
  }
  div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
    color: #f2f4f8 !important;
    font-size: 1.7rem !important;
  }
  /* Dataframe */
  div[data-testid="stDataFrame"] {
    border: 1px solid #2a3140;
    border-radius: 14px;
    overflow: hidden;
  }
  /* Badges */
  .badge {
    display: inline-block;
    padding: 4px 12px;
    border-radius: 999px;
    font-weight: 700;
    font-size: 0.85rem;
    color: #ffffff;
    white-space: nowrap;
  }
  .badge-strong { background-color: #16a34a; }
  .badge-marginal { background-color: #d97706; }
  .badge-pass { background-color: #dc2626; }
  .kpi-note { color: #9aa4b5; font-size: 0.85rem; }
  .guide-box {
    background-color: #161b24;
    border: 1px solid #2a3140;
    border-radius: 14px;
    padding: 18px 22px;
    margin-bottom: 16px;
  }
  .guide-box h3 { margin-top: 0; }
  .formula {
    background-color: #0b0e13;
    border: 1px solid #2e3542;
    border-radius: 10px;
    padding: 10px 14px;
    font-family: monospace;
    font-size: 0.95rem;
    color: #93c5fd;
  }
</style>
"""

st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Underwriting engine
# ---------------------------------------------------------------------------
MAO_FACTOR = 0.70


def compute_underwriting(arv: float, repairs: float, opening_bid: float):
    """Return (mao, net_profit, roi_pct, badge) for a deal.

    MAO        = ARV * 0.70 - Repairs
    Net Profit = ARV - MAO - Repairs  (purchase price assumed to be the MAO)
    ROI %      = Net Profit / (MAO + Repairs) * 100

    Badge rules:
      Strong Buy          ROI >= 20% AND MAO >= opening bid
      Marginal            10% <= ROI < 20%
      Overpriced / Pass   ROI < 10% OR MAO < opening bid
    """
    mao = arv * MAO_FACTOR - repairs
    net_profit = arv - mao - repairs
    invested = mao + repairs
    roi_pct = (net_profit / invested * 100.0) if invested > 0 else 0.0

    if roi_pct < 10.0 or mao < opening_bid:
        badge = "Overpriced / Pass"
    elif roi_pct >= 20.0:
        badge = "Strong Buy"
    else:
        badge = "Marginal"
    return mao, net_profit, roi_pct, badge


def badge_html(badge: str) -> str:
    css_class = {
        "Strong Buy": "badge-strong",
        "Marginal": "badge-marginal",
        "Overpriced / Pass": "badge-pass",
    }[badge]
    return f'<span class="badge {css_class}">{badge}</span>'


# ---------------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------------
def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT NOT NULL,
            county TEXT NOT NULL,
            auction_date TEXT NOT NULL,
            opening_bid REAL NOT NULL,
            max_bid REAL NOT NULL,
            arv REAL NOT NULL,
            repairs REAL NOT NULL,
            mao REAL NOT NULL,
            net_profit REAL NOT NULL,
            roi_pct REAL NOT NULL,
            badge TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def insert_deal(address, county, auction_date, opening_bid, max_bid, arv,
                repairs, mao, net_profit, roi_pct, badge):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO deals
            (address, county, auction_date, opening_bid, max_bid, arv,
             repairs, mao, net_profit, roi_pct, badge, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            address, county, auction_date, opening_bid, max_bid, arv,
            repairs, mao, net_profit, roi_pct, badge,
            time.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()


def delete_deal(deal_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
    conn.commit()
    conn.close()


def load_deals() -> pd.DataFrame:
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM deals ORDER BY auction_date ASC, id DESC", conn)
    conn.close()
    return df


init_db()

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("\U0001f3db\ufe0f Real Estate Auction Pipeline")
st.markdown(
    '<p class="kpi-note">Track auction deals, underwrite them instantly with the '
    "70% rule, and keep the whole pipeline in one place.</p>",
    unsafe_allow_html=True,
)

tab_intake, tab_board, tab_guide = st.tabs(
    ["\U0001f4dd Deal Intake", "\U0001f4ca Deal Board", "\U0001f4d6 Underwriting Guide"]
)

# ---------------------------------------------------------------------------
# Tab 1 — Deal Intake
# ---------------------------------------------------------------------------
with tab_intake:
    st.subheader("Add a New Auction Deal")

    with st.form("deal_intake_form", clear_on_submit=True):
        col_a, col_b = st.columns(2)
        with col_a:
            address = st.text_input("Property Address *", placeholder="1234 W Main St, Chicago, IL 60601")
            county = st.text_input("County *", placeholder="Cook")
            auction_date = st.date_input("Auction Date", value=date.today(), min_value=date(2000, 1, 1))
        with col_b:
            opening_bid = st.number_input("Opening Bid ($)", min_value=0.0, value=0.0, step=1000.0, format="%.2f")
            max_bid = st.number_input("Max Bid ($)", min_value=0.0, value=0.0, step=1000.0, format="%.2f")
            arv = st.number_input("Estimated ARV — After Repair Value ($) *", min_value=0.0, value=0.0, step=1000.0, format="%.2f")
            repairs = st.number_input("Estimated Repair Costs ($)", min_value=0.0, value=0.0, step=1000.0, format="%.2f")

        submitted = st.form_submit_button("Underwrite & Save Deal", use_container_width=True)

    if submitted:
        errors = []
        if not address.strip():
            errors.append("Property address is required.")
        if not county.strip():
            errors.append("County is required.")
        if arv <= 0:
            errors.append("Estimated ARV must be greater than $0.")
        for label, value in [
            ("Opening Bid", opening_bid),
            ("Max Bid", max_bid),
            ("Estimated ARV", arv),
            ("Estimated Repair Costs", repairs),
        ]:
            if value < 0:
                errors.append(f"{label} cannot be negative.")

        if errors:
            for err in errors:
                st.error(err)
        else:
            mao, net_profit, roi_pct, badge = compute_underwriting(arv, repairs, opening_bid)
            insert_deal(
                address.strip(),
                county.strip(),
                auction_date.isoformat(),
                opening_bid,
                max_bid,
                arv,
                repairs,
                mao,
                net_profit,
                roi_pct,
                badge,
            )
            st.success(f"Deal saved: {address.strip()}")

            st.markdown("### Underwriting Result")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Max Allowable Offer (MAO)", f"${mao:,.2f}")
            c2.metric("Estimated Net Profit", f"${net_profit:,.2f}")
            c3.metric("Projected ROI", f"{roi_pct:.1f}%")
            with c4:
                st.markdown("**Deal Quality**")
                st.markdown(badge_html(badge), unsafe_allow_html=True)

            st.markdown(
                '<p class="kpi-note">Purchase price assumed to be the MAO. '
                "Net Profit = ARV &minus; MAO &minus; Repairs. "
                "ROI = Net Profit &divide; (MAO + Repairs) &times; 100.</p>",
                unsafe_allow_html=True,
            )
            st.rerun()

# ---------------------------------------------------------------------------
# Tab 2 — Deal Board
# ---------------------------------------------------------------------------
with tab_board:
    st.subheader("Deal Board")

    deals = load_deals()

    if deals.empty:
        st.info("No deals yet. Add your first deal in the Deal Intake tab.")
    else:
        # KPI metric cards
        total_deals = len(deals)
        avg_roi = float(deals["roi_pct"].mean())
        total_profit = float(deals["net_profit"].sum())
        strong_count = int((deals["badge"] == "Strong Buy").sum())

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total Deals", f"{total_deals}")
        k2.metric("Average ROI", f"{avg_roi:.1f}%")
        k3.metric("Total Projected Profit", f"${total_profit:,.2f}")
        k4.metric("Strong Buy Deals", f"{strong_count}")

        st.markdown("---")

        # Filters
        f1, f2, f3, f4 = st.columns([2, 2, 2, 3])
        with f1:
            counties = ["All Counties"] + sorted(deals["county"].dropna().unique().tolist())
            county_filter = st.selectbox("Filter by County", counties)
        with f2:
            badge_options = ["All Badges", "Strong Buy", "Marginal", "Overpriced / Pass"]
            badge_filter = st.selectbox("Filter by Deal Quality", badge_options)
        with f3:
            sort_options = {
                "Auction Date (soonest)": ("auction_date", True),
                "Auction Date (latest)": ("auction_date", False),
                "ROI (highest)": ("roi_pct", False),
                "ROI (lowest)": ("roi_pct", True),
                "Net Profit (highest)": ("net_profit", False),
                "Net Profit (lowest)": ("net_profit", True),
                "MAO (highest)": ("mao", False),
                "MAO (lowest)": ("mao", True),
            }
            sort_choice = st.selectbox("Sort By", list(sort_options.keys()))
        with f4:
            search_text = st.text_input("Search Address", placeholder="Type part of an address...")

        filtered = deals.copy()
        if county_filter != "All Counties":
            filtered = filtered[filtered["county"] == county_filter]
        if badge_filter != "All Badges":
            filtered = filtered[filtered["badge"] == badge_filter]
        if search_text.strip():
            filtered = filtered[
                filtered["address"].str.contains(search_text.strip(), case=False, na=False)
            ]
        sort_col, sort_asc = sort_options[sort_choice]
        filtered = filtered.sort_values(by=sort_col, ascending=sort_asc)

        st.markdown(f"**Showing {len(filtered)} of {len(deals)} deals**")

        if filtered.empty:
            st.warning("No deals match the current filters.")
        else:
            display = filtered.copy()
            display["Opening Bid"] = display["opening_bid"].map("${:,.2f}".format)
            display["Max Bid"] = display["max_bid"].map("${:,.2f}".format)
            display["ARV"] = display["arv"].map("${:,.2f}".format)
            display["Repairs"] = display["repairs"].map("${:,.2f}".format)
            display["MAO"] = display["mao"].map("${:,.2f}".format)
            display["Net Profit"] = display["net_profit"].map("${:,.2f}".format)
            display["ROI %"] = display["roi_pct"].map("{:.1f}%".format)
            display = display.rename(
                columns={
                    "address": "Address",
                    "county": "County",
                    "auction_date": "Auction Date",
                    "badge": "Deal Quality",
                }
            )
            board_cols = [
                "Address", "County", "Auction Date", "Opening Bid", "Max Bid",
                "ARV", "Repairs", "MAO", "Net Profit", "ROI %", "Deal Quality",
            ]
            st.dataframe(display[board_cols], use_container_width=True, hide_index=True)

            # CSV export of the currently filtered board
            export_df = filtered[
                [
                    "address", "county", "auction_date", "opening_bid", "max_bid",
                    "arv", "repairs", "mao", "net_profit", "roi_pct", "badge",
                    "created_at",
                ]
            ].copy()
            export_df.columns = [
                "Address", "County", "Auction Date", "Opening Bid", "Max Bid",
                "ARV", "Repairs", "MAO", "Net Profit", "ROI %", "Deal Quality",
                "Created At",
            ]
            csv_bytes = export_df.to_csv(index=False).encode("utf-8")
            st.download_button(
                label="\u2b07\ufe0f Download Filtered Board (CSV)",
                data=csv_bytes,
                file_name="auction_deals.csv",
                mime="text/csv",
                use_container_width=True,
            )

        # Per-deal delete
        st.markdown("---")
        st.subheader("Manage Deals")
        st.markdown(
            '<p class="kpi-note">Select a deal below and delete it. '
            "This permanently removes it from auctions.db.</p>",
            unsafe_allow_html=True,
        )
        delete_labels = [
            f"#{row.id} — {row.address} ({row.county}, {row.auction_date})"
            for row in deals.itertuples()
        ]
        delete_choice = st.selectbox("Select a deal to delete", ["— Select a deal —"] + delete_labels)
        if delete_choice != "— Select a deal —":
            deal_id = int(delete_choice.split("—")[0].strip().lstrip("#"))
            if st.button("Delete Selected Deal", type="secondary", use_container_width=True):
                delete_deal(deal_id)
                st.success("Deal deleted.")
                st.rerun()

# ---------------------------------------------------------------------------
# Tab 3 — Underwriting Guide
# ---------------------------------------------------------------------------
with tab_guide:
    st.subheader("Underwriting Guide")

    st.markdown(
        """
        <div class="guide-box">
          <h3>The 70% Rule</h3>
          <p>Never pay more than 70% of a property's After Repair Value, minus the
          cost of repairs. The 30% margin covers holding costs, closing costs,
          selling costs, financing, and your profit.</p>
          <div class="formula">MAO = ARV &times; 0.70 &minus; Repairs</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="guide-box">
          <h3>Estimated Net Profit</h3>
          <p>Assuming you buy at the Max Allowable Offer, your profit is what is
          left of the ARV after paying the purchase price and the repair bill.</p>
          <div class="formula">Net Profit = ARV &minus; MAO &minus; Repairs</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="guide-box">
          <h3>Projected ROI</h3>
          <p>ROI measures the profit against everything you put in: the purchase
          price (MAO) plus the repairs. A higher ROI means the deal returns more
          for each dollar invested.</p>
          <div class="formula">ROI % = Net Profit &divide; (MAO + Repairs) &times; 100</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="guide-box">
          <h3>Deal Quality Badge</h3>
          <p>The badge is assigned automatically from the ROI and the opening bid:</p>
          <ul>
            <li><b>Strong Buy</b> — ROI of 20% or more <i>and</i> the MAO is at or above the opening bid. The deal clears your profit bar and is winnable at auction.</li>
            <li><b>Marginal</b> — ROI between 10% and 20%. Workable, but thin. Re-check your ARV and repair estimates before bidding.</li>
            <li><b>Overpriced / Pass</b> — ROI below 10%, or the MAO is below the opening bid (you would have to overpay to win). Walk away unless the numbers change.</li>
          </ul>
          <p>Badge thresholds are fixed so every deal is judged the same way:
          Strong Buy needs ROI &ge; 20% with MAO &ge; opening bid; Marginal needs
          ROI &ge; 10%; anything else is Overpriced / Pass.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="guide-box">
          <h3>Worked Example</h3>
          <p>ARV $300,000, repairs $50,000, opening bid $140,000:</p>
          <div class="formula">MAO = $300,000 &times; 0.70 &minus; $50,000 = $160,000</div>
          <div class="formula">Net Profit = $300,000 &minus; $160,000 &minus; $50,000 = $90,000</div>
          <div class="formula">ROI = $90,000 &divide; ($160,000 + $50,000) &times; 100 = 42.9%</div>
          <p>ROI is 42.9% and the MAO ($160,000) is above the opening bid ($140,000),
          so this deal earns a <b>Strong Buy</b> badge.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Pipeline Overview")
    all_deals = load_deals()
    if all_deals.empty:
        st.markdown("No deals in the pipeline yet.")
    else:
        st.metric("Deals in Pipeline", len(all_deals))
        st.metric("Strong Buy Deals", int((all_deals["badge"] == "Strong Buy").sum()))
        st.metric("Total Projected Profit", f"${float(all_deals['net_profit'].sum()):,.2f}")
        st.markdown("---")
        st.markdown("**Next Auction**")
        next_deal = all_deals.sort_values("auction_date").iloc[0]
        st.markdown(f"**{next_deal['address']}**")
        st.markdown(f"{next_deal['county']} — {next_deal['auction_date']}")
        st.markdown(badge_html(next_deal["badge"]), unsafe_allow_html=True)
    st.markdown("---")
    st.markdown(
        '<p class="kpi-note">Data is stored locally in auctions.db. '
        "Nothing leaves this machine.</p>",
        unsafe_allow_html=True,
    )
