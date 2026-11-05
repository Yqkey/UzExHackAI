"""Собирает data/exchange.duckdb из CSV. Запуск: python build_db.py [папка_с_csv]"""
import sys, re, pathlib, duckdb, pandas as pd
import normalize as N

SRC = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads")
OUT = pathlib.Path(__file__).parent / "data" / "exchange.duckdb"
OUT.parent.mkdir(exist_ok=True); OUT.unlink(missing_ok=True)

k = pd.read_csv(SRC/"contracts.csv", dtype=str)
def row(r):
    name = r["Наименование продукта"]
    base = re.sub(r"\((спот|форвард|фьючерс)\)", "", name, flags=re.I)
    g = lambda c: N._clean(r[c])
    sort = N.norm_sort(g("Сорт товара")) or (N.norm_sort(m.group(0)) if (m := N.SORT_RE.search(base)) else None)
    cls  = N.norm_class(g("Класс товара")) or (N.norm_class(m.group(0)) if (m := N.CLASS_RE.search(base)) else None)
    mark, size, typ = N.norm_token(g("Марка товара")), N.norm_token(g("Размер товара")), N.norm_type(g("Тип товара"))
    b = N.norm_token(base)
    if not mark:
        m = re.search(r"(ЦЕМ[IV/АВ\-ШК]*\d+,\d+Н|АИ-\d+|PE-\d+|LL-\d+|HD-\d+|PP-\d+|БНД\d+/\d+|Г-\d|М-?\d+К?|М\d+К|ПФ-\d+|NE\d+/1|ВР-1)", b, re.I)
        mark = m.group(1).upper() if m else None
    if not size:
        m = re.search(r"(Ø?\d+(?:,\d+)?(?:Х\d+)*ММ)", b) or re.search(r"(\d+)ММ", b)
        size = m.group(1) if m else None
    if not typ:
        for t in ["хлебопекарная","обогащенная витаминно-минеральной смесью","рафинированное дезодорированное","рафинированное","тостированный","обрезные хвойных пород","дорожный"]:
            if t.replace(" ","").upper() in b: typ = t; break
    return pd.Series({"product": N.canon_product(name), "product_raw": name.strip(),
                      "sort": sort, "class": cls, "mark": mark, "size": size, "type": typ})
extra = k.apply(row, axis=1)
k = pd.concat([k, extra], axis=1)

con = duckdb.connect(str(OUT))
con.register("k_df", k)
con.execute("""create table contracts as select
  cast("Номер контракта" as bigint) contract_id, product, product_raw, "sort", "class", mark, size, "type",
  "Код ТН ВЭД" hs_code, "Группа товара" product_group, "Категория товара" category, "Отрасль промышленности" industry,
  "Единица измерения" unit, cast("Лот" as integer) lot, "Валюта" currency, cast("Базисная цена" as double) base_price,
  "Сумма залога" deposit, "Базис поставки" incoterm, "Регион" region_ru, "Адрес склада" warehouse_address,
  "Тип торгов" auction_type, "Тип контракта" contract_type, "Форма контракта" contract_form,
  cast("Срок оплаты" as integer) payment_days, cast("Срок поставки" as integer) delivery_days, "Упаковка" packaging,
  "ИНН продавца" seller_inn, strptime("Дата регистрации",'%d.%m.%Y')::date reg_date, strptime("Действует до",'%d.%m.%Y')::date valid_until
  from k_df""")
con.register("reg_df", pd.DataFrame(N.REGIONS, columns=["region_uz","region_ru"]))
con.execute("create table regions as select * from reg_df")
glob = [str(SRC/f"deals_{y}.csv") for y in range(2022, 2027)]
con.execute(f"""create table deals as select
  cast("Номер сделки" as bigint) deal_id,
  strptime("Дата сделки",'%d.%m.%Y')::date deal_date, "Время сделки"::time deal_time,
  "Тип торгов" auction_type, cast("Количество" as bigint) qty,
  cast(replace("Сумма сделки",' ','') as double) amount, cast(replace("Стартовая цена",' ','') as double) start_price,
  cast("Номер контракта" as bigint) contract_id, "Наименование товара" product_full_name,
  cast("Лот" as integer) lot, "Единица измерения" unit, "Регион" region_uz,
  "Тип контракта" contract_type, "Форма контракта" contract_form, "Состояние сделки" state,
  "ИНН продавца" seller_inn, "ИНН покупателя" buyer_inn, "Брокер продавца" seller_broker, "Брокер покупателя" buyer_broker,
  try_strptime("Дата оплаты",'%d.%m.%Y')::date pay_date, try_strptime("Дата поставки",'%d.%m.%Y')::date delivery_date,
  cast("Поставлено" as bigint) delivered_qty
  from read_csv({glob}, header=true, all_varchar=true, union_by_name=true)""")
con.execute(f"""create table participants as select "ИНН" inn, "Наименование" name, "Регион" region_uz, "Вид деятельности" activity,
  "Роль" role, "Плательщик НДС" vat_payer from read_csv('{SRC/'participants.csv'}', header=true, all_varchar=true)""")
con.execute(f"""create table brokers as select "Код брокера" broker_code, "Наименование" name, "ИНН" inn, "Регион" region_uz
  from read_csv('{SRC/'brokers.csv'}', header=true, all_varchar=true)""")
con.execute(f"""create table usd_rates as select strptime("Дата",'%d.%m.%Y')::date rate_date, cast("Курс" as double) rate
  from read_csv('{SRC/'usd_rates.csv'}', header=true, all_varchar=true)""")

# Единая широкая витрина — с ней работает агент.
con.execute("""create view v_deals as select
  d.deal_id, d.deal_date, d.deal_time, extract(year from d.deal_date)::int as year, extract(month from d.deal_date)::int as month,
  d.state, (d.state <> 'Аннулирован(Не оплачен)') as is_valid,
  d.auction_type, d.contract_type, d.contract_form, d.contract_id,
  c.product, c.product_raw, c.sort, c.class, c.mark, c.size, c.type, c.category, c.industry, c.product_group, c.hs_code,
  d.unit, d.lot, d.qty, d.qty*d.lot as volume,
  c.currency, d.amount, d.amount/(d.qty*d.lot) as unit_price, d.start_price,
  case when c.currency='Доллар США' then d.amount*r.rate else d.amount end as amount_uzs,
  case when c.currency='Доллар США' then d.amount else d.amount/r.rate end as amount_usd,
  d.region_uz, reg.region_ru,
  d.seller_inn, sp.name as seller_name, sp.activity as seller_activity, sp.region_uz as seller_region,
  d.buyer_inn, bp.name as buyer_name, bp.activity as buyer_activity, bp.region_uz as buyer_region,
  d.seller_broker, sb.name as seller_broker_name, d.buyer_broker, bb.name as buyer_broker_name,
  d.pay_date, d.delivery_date, d.delivered_qty, d.delivered_qty*d.lot as delivered_volume,
  c.payment_days, c.delivery_days, c.base_price
  from deals d
  join contracts c using (contract_id)
  left join usd_rates r on r.rate_date = d.deal_date
  left join regions reg on reg.region_uz = d.region_uz
  left join participants sp on sp.inn = d.seller_inn
  left join participants bp on bp.inn = d.buyer_inn
  left join brokers sb on sb.broker_code = d.seller_broker
  left join brokers bb on bb.broker_code = d.buyer_broker""")
n = con.execute("select count(*) from v_deals").fetchone()[0]
print("v_deals rows:", n)
assert n == 500000
con.close()
