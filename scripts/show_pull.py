import sys
import pandas as pd

df = pd.read_csv(sys.argv[1])
print(f"{len(df)} unique jobs")
print()
for _, r in df.iterrows():
    sal = "n/a"
    if pd.notna(r.get("min_amount")):
        sal = f"${r.min_amount:,.0f}-${r.max_amount:,.0f}" if pd.notna(r.get("max_amount")) else f"${r.min_amount:,.0f}"
    print(f"  {r.title} | {r.company} | {r.location} | {sal} | {r.date_posted}")
