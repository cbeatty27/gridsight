import duckdb

con = duckdb.connect("data/gridsight.duckdb")

con.execute("""
    CREATE OR REPLACE TABLE zone_demand AS
    SELECT
        strptime(period, '%Y-%m-%dT%H') AS hour_utc,
        zone,
        demand_mw
    FROM read_csv(
        'data/processed/zone_demand.csv',
        header = true,
        columns = {'period': 'VARCHAR', 'zone': 'VARCHAR', 'demand_mw': 'DOUBLE'}
    )
""")

print("Overall check:")
print(con.execute("""
    SELECT COUNT(*) AS rows,
           COUNT(DISTINCT zone) AS zones,
           MIN(hour_utc) AS first_hour,
           MAX(hour_utc) AS last_hour
    FROM zone_demand
""").df())

print("\nBy zone:")
print(con.execute("""
    SELECT zone,
           COUNT(*) AS hours,
           MAX(demand_mw) AS peak_mw,
           ROUND(AVG(demand_mw)) AS avg_mw
    FROM zone_demand
    GROUP BY zone
    ORDER BY peak_mw DESC
""").df().to_string())