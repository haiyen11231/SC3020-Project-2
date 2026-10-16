# ============================================================
# SC4020 Project 2 - Task 1: data cleaning + exploration
# Datasets: DAVIS (pKd) and KIBA, from Kaggle (Source 1)
# Run: python clean.py   (needs pandas, seaborn, scikit-learn)
# ============================================================

# ---- Cell 1: imports, folders, load data ----
import numpy as np, pandas as pd, seaborn as sns, matplotlib.pyplot as plt
from pathlib import Path
from sklearn.decomposition import PCA
from sklearn.cluster import KMeans

display = print

# make folders for saving stuff so teammates can find it
Path("data").mkdir(exist_ok=True)
Path("figures").mkdir(exist_ok=True)
Path("outputs").mkdir(exist_ok=True)

raw = {
    "DAVIS": pd.read_csv("data/davis_all.csv"),
    "KIBA":  pd.read_csv("data/kiba_all.csv"),
}
for k, df in raw.items():
    # kaggle files have 3 columns, rename so both datasets look the same
    df.columns = ["smiles", "seq", "score"]
    print(k, df.shape)
    display(df.head(3))   # quick look at the data format for the report table

# ---- Cell 2: cleaning + before/after counts ----
def keys_ok(df):
    # True if drug and protein are both present and not just spaces
    return (df.smiles.notna() & df.seq.notna()
            & (df.smiles.astype(str).str.strip() != "")
            & (df.seq.astype(str).str.strip() != ""))

def clean(df, name=None):
    d = df.copy()
    # force score to numbers, anything weird becomes NaN
    d["score"] = pd.to_numeric(d["score"], errors="coerce")
    # drop NaN and inf scores (non-finite)
    d = d[np.isfinite(d["score"])]
    # drop rows with a missing or blank (whitespace only) drug or protein
    d = d[keys_ok(d)]
    if name == "DAVIS":
        # Kd = 10^(9 - pKd) so it is always positive if pKd is finite
        # (note: this only holds if the scores really are pKd already)
        assert (10 ** (9 - d["score"]) > 0).all()
    # same drug + same protein repeated -> average the scores into one row
    # NOTE: zeros and the 5.0 floor are NOT removed, brief says keep them
    d = d.groupby(["smiles", "seq"], as_index=False)["score"].mean()
    return d

clean_df, rows = {}, []
for k, df in raw.items():
    c = clean(df, k)
    clean_df[k] = c
    rows.append([k, "original", len(df), df.smiles.nunique(), df.seq.nunique()])
    rows.append([k, "cleaned",  len(c),  c.smiles.nunique(),  c.seq.nunique()])
counts = pd.DataFrame(rows, columns=["dataset", "stage", "pairs", "ligands", "targets"])
display(counts)

# sparsity = missing cells / all cells in the ligand x target grid
for k, c in clean_df.items():
    L, T = c.smiles.nunique(), c.seq.nunique()
    print(f"{k}: matrix {L}x{T}, sparsity = {1 - len(c)/(L*T):.3f}")

# ---- Cell 3: heatmaps (missing cells shown in grey) ----
fig, axes = plt.subplots(1, 2, figsize=(16, 6))
for ax, (k, c) in zip(axes, clean_df.items()):
    # pivot to a ligand x target grid, empty cells become NaN
    mat = c.pivot(index="smiles", columns="seq", values="score")
    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad("lightgrey")   # grey = not measured (NOT zero!)
    sns.heatmap(mat, mask=mat.isna(), cmap=cmap, ax=ax,
                xticklabels=False, yticklabels=False,
                cbar_kws={"label": "pKd" if k == "DAVIS" else "KIBA score"})
    ax.set_title(f"{k} ({mat.shape[0]} ligands x {mat.shape[1]} targets)")
    ax.set_xlabel("targets"); ax.set_ylabel("ligands")
plt.tight_layout()
plt.savefig("figures/heatmaps.png", dpi=200, bbox_inches="tight")
plt.show()

# ---- Cell 4: KDE of score distributions ----
fig, axes = plt.subplots(1, 2, figsize=(12, 4))
for ax, (k, c) in zip(axes, clean_df.items()):
    sns.kdeplot(c["score"], fill=True, ax=ax)
    ax.set_xlabel("pKd" if k == "DAVIS" else "KIBA score")
    ax.set_ylabel("density")
    ax.set_title(f"{k} score distribution")
plt.tight_layout()
plt.savefig("figures/kde.png", dpi=200, bbox_inches="tight")
plt.show()

# ---- Cell 5: weak binder check (we discuss these, never silently delete) ----
d = clean_df["DAVIS"]
# 5.0 = Kd of 10,000 nM, the assay limit, so "too weak to measure"
print("DAVIS share at pKd<=5.0:", (d.score <= 5.0 + 1e-9).mean())
print(clean_df["KIBA"].score.describe())   # min is 0.0, kept as an extreme value

# ---- Cell 6: analytical tools (PCA, KMeans, clustermap, KIBA counts) ----
dm = clean_df["DAVIS"].pivot(index="smiles", columns="seq", values="score")
X = dm.values   # DAVIS is a full grid so no NaN here, safe for PCA/KMeans

Z = PCA(n_components=2, random_state=42).fit_transform(X)            # squash to 2D
lab = KMeans(n_clusters=4, n_init=10, random_state=42).fit_predict(X)  # group ligands

fig, axes = plt.subplots(1, 2, figsize=(12, 4))
axes[0].scatter(Z[:, 0], Z[:, 1], c=lab, cmap="tab10")
axes[0].set_title("DAVIS ligands: PCA + KMeans (k=4)")
axes[0].set_xlabel("PC1"); axes[0].set_ylabel("PC2")
# how many targets does each ligand barely bind (at the 5.0 floor)?
axes[1].hist((dm <= 5.0 + 1e-9).mean(axis=1), bins=20)
axes[1].set_title("DAVIS: share of targets at pKd floor, per ligand")
axes[1].set_xlabel("fraction at 5.0"); axes[1].set_ylabel("ligands")
plt.tight_layout()
plt.savefig("figures/pca_floor.png", dpi=200, bbox_inches="tight")
plt.show()

# hierarchical clustering: similar ligands/targets get placed next to each other
g = sns.clustermap(dm, cmap="viridis", xticklabels=False, yticklabels=False,
                   method="average", figsize=(9, 8))
g.savefig("figures/davis_clustermap.png", dpi=200, bbox_inches="tight")
plt.show()

# KIBA: how many measurements does each ligand / target have?
kb = clean_df["KIBA"]
fig, axes = plt.subplots(1, 2, figsize=(12, 4))
axes[0].hist(kb.groupby("smiles").size(), bins=40)
axes[0].set_title("KIBA: observations per ligand")
axes[0].set_xlabel("count"); axes[0].set_ylabel("ligands")
axes[1].hist(kb.groupby("seq").size(), bins=40)
axes[1].set_title("KIBA: observations per target")
axes[1].set_xlabel("count"); axes[1].set_ylabel("targets")
plt.tight_layout()
plt.savefig("figures/kiba_counts.png", dpi=200, bbox_inches="tight")
plt.show()

# ---- Cell 7: cleaning log, final checks, export for teammates ----
# split the row drop into reasons so the report is clear
log = []
for k, df in raw.items():
    s = pd.to_numeric(df["score"], errors="coerce")
    fin = np.isfinite(s)                              # score is a real number
    ok = keys_ok(df)                                  # drug + protein not blank
    valid = (fin & ok).sum()
    log.append([k, len(df),
                (~fin).sum(),                  # rows with bad score
                (fin & ~ok).sum(),             # rows with blank smiles/seq
                valid - len(clean_df[k]),      # repeated pairs merged by averaging
                len(clean_df[k])])             # final pair count
cleanlog = pd.DataFrame(log, columns=["dataset", "original_rows", "invalid_score",
                                      "blank_smiles_or_seq", "duplicates_merged",
                                      "final_pairs"])
print(cleanlog)

# sanity checks before saving
for k, c in clean_df.items():
    assert not c.duplicated(["smiles", "seq"]).any()   # no repeated pairs left
    assert np.isfinite(c.score).all()                  # all scores valid
    assert keys_ok(c).all()                            # no blanks
    c.to_csv(f"outputs/{k.lower()}_clean.csv", index=False)
    raw[k].head(5).to_csv(f"outputs/{k.lower()}_examples.csv", index=False)
assert (clean_df["KIBA"].score == 0).any()   # KIBA zero scores were kept

counts.to_csv("outputs/counts_table.csv", index=False)
cleanlog.to_csv("outputs/cleaning_log.csv", index=False)
print("saved figures/ and outputs/")