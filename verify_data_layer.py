"""Независимая сверка витрины v_deals с расчётом напрямую по сырым CSV (pandas, другой код)."""
import pandas as pd, duckdb, sys, glob
SRC = sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads"
raw = pd.concat([pd.read_csv(f, dtype=str) for f in sorted(glob.glob(f"{SRC}/deals_*.csv"))])
k = pd.read_csv(f"{SRC}/contracts.csv", dtype=str)[["Номер контракта","Валюта"]]
r = pd.read_csv(f"{SRC}/usd_rates.csv", dtype=str)
raw["amt"] = raw["Сумма сделки"].str.replace(" ", "").astype(float)
raw = raw.merge(k, on="Номер контракта").merge(r, left_on="Дата сделки", right_on="Дата", how="left")
raw["uzs"] = raw.amt * raw["Курс"].astype(float).where(raw["Валюта"] == "Доллар США", 1.0)
raw["year"] = raw["Дата сделки"].str[-4:]
raw["vol"] = raw["Количество"].astype(float) * raw["Лот"].astype(float)
ok = raw[raw["Состояние сделки"] != "Аннулирован(Не оплачен)"]
con = duckdb.connect("data/exchange.duckdb", read_only=True)
checks = []
# 1) сумма в сумах по годам
a = ok.groupby("year").uzs.sum()
b = dict(con.execute("select year, sum(amount_uzs) from v_deals where is_valid group by 1").fetchall())
checks += [(f"sum_uzs {y}", a[y], b[int(y)]) for y in a.index]
# 2) число сделок по регионам и состояниям
a = raw.groupby("Регион").size(); b = dict(con.execute("select region_uz, count(*) from v_deals group by 1").fetchall())
checks += [(f"cnt region {x}", a[x], b[x]) for x in a.index]
a = raw.groupby("Состояние сделки").size(); b = dict(con.execute("select state, count(*) from v_deals group by 1").fetchall())
checks += [(f"cnt state {x}", a[x], b[x]) for x in a.index]
# 3) топ-продавец по сумме (ИНН)
a = ok.groupby("ИНН продавца").uzs.sum().sort_values(ascending=False).head(3)
b = con.execute("select seller_inn, sum(amount_uzs) s from v_deals where is_valid group by 1 order by s desc limit 3").fetchall()
checks += [(f"top seller #{i+1} {inn}", v, dict(b).get(inn)) for i, (inn, v) in enumerate(a.items())]
# 4) объём
a = ok.groupby("Единица измерения").vol.sum(); b = dict(con.execute("select unit, sum(volume) from v_deals where is_valid group by 1").fetchall())
checks += [(f"volume {u}", a[u], b[u]) for u in a.index]
bad = [(n, x, y) for n, x, y in checks if y is None or abs(x - y) > max(0.01, abs(x) * 1e-9)]
print(f"проверок: {len(checks)}, расхождений: {len(bad)}"); [print("  ✗", t) for t in bad]
sys.exit(1 if bad else 0)
