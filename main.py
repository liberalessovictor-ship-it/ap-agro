"""
API — Plataforma de Agricultura de Precisão (item 1 + item 2 do roteiro)
Roda sobre o PostgreSQL/PostGIS já populado com os dados reais do GraxaimFrente.

Rodar localmente:
    uvicorn main:app --reload --port 8000

Docs interativas automáticas em /docs (Swagger) assim que estiver no ar.
"""
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
import os, re, io, csv
from shapely.geometry import shape as shp_shape

# Lê configuração do banco de variáveis de ambiente (com defaults para rodar localmente).
# Em produção (Supabase/Railway/Render/Neon), defina essas variáveis no painel do serviço.
DB = dict(
    host=os.getenv("DB_HOST", "localhost"),
    dbname=os.getenv("DB_NAME", "agriprecisao"),
    user=os.getenv("DB_USER", "postgres"),
    password=os.getenv("DB_PASSWORD", "agroprecisao"),
    port=os.getenv("DB_PORT", "5432"),
)

app = FastAPI(
    title="Agricultura de Precisão — API",
    description="Backend espacial (PostGIS) para talhões, amostras de solo, classificação e prescrições.",
    version="0.1.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@contextmanager
def get_cursor():
    conn = psycopg2.connect(**DB)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        conn.commit()
    finally:
        conn.close()


@app.get("/")
def root():
    return {"status": "ok", "service": "agricultura-precisao-api"}


@app.get("/propriedades")
def listar_propriedades():
    with get_cursor() as cur:
        cur.execute("""
            SELECT p.id, p.nome, p.produtor, p.municipio, p.estado,
                   count(DISTINCT t.id) AS n_talhoes
            FROM propriedades p
            LEFT JOIN fazendas f ON f.propriedade_id = p.id
            LEFT JOIN talhoes t ON t.fazenda_id = f.id
            GROUP BY p.id
            ORDER BY p.nome;
        """)
        return cur.fetchall()


@app.post("/propriedades")
def criar_propriedade(nome: str = Form(...), produtor: str = Form(""), municipio: str = Form(""), estado: str = Form("")):
    """Cria uma propriedade. Sem sistema de login ainda (item futuro do roteiro),
    então toda propriedade fica associada ao primeiro usuário cadastrado no banco."""
    with get_cursor() as cur:
        cur.execute("SELECT id FROM usuarios ORDER BY criado_em LIMIT 1;")
        u = cur.fetchone()
        if not u:
            raise HTTPException(400, "Nenhum usuário no banco — rode o seed.py ao menos uma vez.")
        cur.execute("""INSERT INTO propriedades (usuario_id, nome, produtor, municipio, estado)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id, nome;""",
                    (u["id"], nome, produtor, municipio, estado))
        return cur.fetchone()


@app.post("/propriedades/{propriedade_id}/fazendas")
def criar_fazenda(propriedade_id: str, nome: str = Form(...)):
    with get_cursor() as cur:
        cur.execute("""INSERT INTO fazendas (propriedade_id, nome) VALUES (%s,%s)
                       RETURNING id, nome;""", (propriedade_id, nome))
        return cur.fetchone()


@app.get("/propriedades/{propriedade_id}/fazendas")
def listar_fazendas(propriedade_id: str):
    with get_cursor() as cur:
        cur.execute("SELECT id, nome FROM fazendas WHERE propriedade_id = %s ORDER BY nome;", (propriedade_id,))
        return cur.fetchall()


def _parse_kml_polygon(kml_text: str):
    """Extrai o primeiro polígono <coordinates> de um KML e devolve WKT (lon lat, ...)."""
    m = re.search(r"<coordinates>(.*?)</coordinates>", kml_text, re.S)
    if not m:
        raise HTTPException(400, "KML sem tag <coordinates>.")
    pares = m.group(1).strip().split()
    coords = []
    for p in pares:
        partes = p.split(",")
        coords.append((float(partes[0]), float(partes[1])))
    return "POLYGON((" + ",".join(f"{lon} {lat}" for lon, lat in coords) + "))"


@app.post("/fazendas/{fazenda_id}/talhoes")
async def criar_talhao(fazenda_id: str, nome: str = Form(...), sistema_plantio: str = Form(""),
                        kml_file: UploadFile | None = File(None), geojson_polygon: str | None = Form(None)):
    """
    Cria um talhão a partir de um arquivo KML (upload) OU de um GeoJSON de
    polígono colado como texto (campo geojson_polygon, formato
    [[lon,lat],[lon,lat],...]). Suporta os dois caminhos citados no roteiro
    original (import de arquivo vs. desenho/colagem manual).
    """
    if kml_file is not None:
        content = (await kml_file.read()).decode("utf-8", errors="ignore")
        wkt = _parse_kml_polygon(content)
    elif geojson_polygon:
        import json as _json
        coords = _json.loads(geojson_polygon)
        wkt = "POLYGON((" + ",".join(f"{lon} {lat}" for lon, lat in coords) + "))"
    else:
        raise HTTPException(400, "Envie kml_file ou geojson_polygon.")

    with get_cursor() as cur:
        cur.execute("""INSERT INTO talhoes (fazenda_id, nome, geom, sistema_plantio)
                       VALUES (%s,%s, ST_GeomFromText(%s,4326), %s)
                       RETURNING id, nome, area_ha;""", (fazenda_id, nome, wkt, sistema_plantio))
        return cur.fetchone()


@app.get("/talhoes")
def listar_talhoes():
    """Lista todos os talhões cadastrados — alimenta o seletor de talhão do app."""
    with get_cursor() as cur:
        cur.execute("""
            SELECT t.id, t.nome, t.area_ha, f.nome AS fazenda, p.nome AS propriedade,
                   (SELECT count(*) FROM safras s WHERE s.talhao_id = t.id) AS n_safras,
                   (SELECT count(*) FROM amostras a WHERE a.talhao_id = t.id) AS n_amostras
            FROM talhoes t
            JOIN fazendas f ON f.id = t.fazenda_id
            JOIN propriedades p ON p.id = f.propriedade_id
            ORDER BY p.nome, f.nome, t.nome;
        """)
        return cur.fetchall()


@app.post("/talhoes/{talhao_id}/safras")
def criar_safra(talhao_id: str, nome: str = Form(...), cultura: str = Form(""),
                 cultivar: str = Form(""), produtividade_esperada: float | None = Form(None)):
    with get_cursor() as cur:
        cur.execute("""INSERT INTO safras (talhao_id, nome, cultura, cultivar, produtividade_esperada)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id, nome, cultura;""",
                    (talhao_id, nome, cultura, cultivar, produtividade_esperada))
        return cur.fetchone()


@app.get("/talhoes/{talhao_id}/safras")
def listar_safras(talhao_id: str):
    with get_cursor() as cur:
        cur.execute("""
            SELECT s.id, s.nome, s.cultura, s.cultivar, s.produtividade_esperada,
                   (SELECT count(*) FROM amostras a WHERE a.safra_id = s.id) AS n_amostras
            FROM safras s WHERE s.talhao_id = %s ORDER BY s.data_inicio DESC NULLS LAST, s.nome DESC;
        """, (talhao_id,))
        return cur.fetchall()


@app.post("/talhoes/{talhao_id}/safras/{safra_id}/amostras/importar")
async def importar_amostras_csv(talhao_id: str, safra_id: str, arquivo: UploadFile = File(...),
                                 col_numero: str = Form(...), col_lat: str = Form(...),
                                 col_lon: str = Form(...), col_prof: str | None = Form(None)):
    """
    Importa amostras de um CSV "largo" (uma coluna por atributo — igual a um
    laudo de laboratório exportado em planilha). O chamador informa quais
    colunas são número do ponto / latitude / longitude; todas as demais
    colunas numéricas viram registros em analises_laboratoriais.
    """
    raw = (await arquivo.read()).decode("utf-8-sig", errors="ignore")
    reader = csv.DictReader(io.StringIO(raw))
    fieldnames = reader.fieldnames or []
    for req in (col_numero, col_lat, col_lon):
        if req not in fieldnames:
            raise HTTPException(400, f"Coluna '{req}' não encontrada no CSV. Colunas disponíveis: {fieldnames}")
    if len({col_numero, col_lat, col_lon}) < 3:
        raise HTTPException(400, "As colunas de número/latitude/longitude precisam ser diferentes entre si.")
    atributo_cols = [c for c in fieldnames if c not in (col_numero, col_lat, col_lon, col_prof)]

    n_amostras = n_analises = 0
    with get_cursor() as cur:
        for row in reader:
            try:
                lat = float(str(row[col_lat]).replace(",", "."))
                lon = float(str(row[col_lon]).replace(",", "."))
            except (TypeError, ValueError):
                continue
            prof = None
            if col_prof and row.get(col_prof):
                try:
                    prof = float(str(row[col_prof]).replace(",", "."))
                except ValueError:
                    prof = None
            cur.execute("""INSERT INTO amostras (talhao_id, safra_id, numero_ponto, geom, profundidade_cm)
                           VALUES (%s,%s,%s, ST_SetSRID(ST_MakePoint(%s,%s),4326), %s)
                           RETURNING id;""", (talhao_id, safra_id, str(row[col_numero]), lon, lat, prof or 20))
            amostra_id = cur.fetchone()["id"]
            n_amostras += 1
            for col in atributo_cols:
                val = row.get(col)
                if val in (None, ""):
                    continue
                try:
                    v = float(str(val).replace(",", "."))
                except ValueError:
                    continue
                cur.execute("""INSERT INTO analises_laboratoriais (amostra_id, atributo, valor)
                               VALUES (%s,%s,%s);""", (amostra_id, col, v))
                n_analises += 1
    return {"amostras_importadas": n_amostras, "resultados_importados": n_analises}


@app.get("/talhoes/{talhao_id}/safras/comparar")
def comparar_safras(talhao_id: str, safra_a: str, safra_b: str, atributo: str):
    """Compara a média (e classe) de um atributo entre duas safras do mesmo talhão."""
    with get_cursor() as cur:
        def resumo(safra_id):
            cur.execute("""
                SELECT s.nome, AVG(al.valor) AS media, MIN(al.valor) AS minimo, MAX(al.valor) AS maximo,
                       COUNT(al.valor) AS n
                FROM safras s
                LEFT JOIN amostras a ON a.safra_id = s.id
                LEFT JOIN analises_laboratoriais al ON al.amostra_id = a.id AND al.atributo = %s
                WHERE s.id = %s
                GROUP BY s.nome;
            """, (atributo, safra_id))
            return cur.fetchone()
        ra, rb = resumo(safra_a), resumo(safra_b)
        if not ra or not rb:
            raise HTTPException(404, "Safra não encontrada.")
        return {"atributo": atributo, "safra_a": ra, "safra_b": rb}


@app.get("/talhoes/_first")
def get_primeiro_talhao():
    """
    Atalho de conveniência para demos com um único talhão: evita que o
    front-end precise conhecer o UUID de antemão. Numa versão com múltiplos
    talhões, isso vira um seletor na UI que lista /propriedades e navega.
    """
    with get_cursor() as cur:
        cur.execute("SELECT id, nome FROM talhoes ORDER BY criado_em LIMIT 1;")
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Nenhum talhão cadastrado ainda. Rode o seed.py.")
        return row


@app.get("/talhoes/{talhao_id}")
def get_talhao(talhao_id: str):
    with get_cursor() as cur:
        cur.execute("""
            SELECT t.id, t.nome, t.area_ha, t.sistema_plantio,
                   ST_AsGeoJSON(t.geom)::json AS geometria,
                   f.nome AS fazenda, p.nome AS propriedade,
                   (SELECT count(*) FROM amostras a WHERE a.talhao_id = t.id) AS n_amostras
            FROM talhoes t
            JOIN fazendas f ON f.id = t.fazenda_id
            JOIN propriedades p ON p.id = f.propriedade_id
            WHERE t.id = %s;
        """, (talhao_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Talhão não encontrado")
        return row


@app.get("/talhoes/{talhao_id}/elementos")
def listar_elementos(talhao_id: str):
    """Lista os atributos de solo disponíveis para esse talhão (para popular um <select>)."""
    with get_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT al.atributo, al.unidade
            FROM analises_laboratoriais al
            JOIN amostras a ON a.id = al.amostra_id
            WHERE a.talhao_id = %s
            ORDER BY al.atributo;
        """, (talhao_id,))
        return cur.fetchall()


@app.get("/talhoes/{talhao_id}/amostras")
def listar_amostras(talhao_id: str, atributo: str | None = None):
    """
    Amostras do talhão com geometria (GeoJSON), e opcionalmente o valor + classe
    de um atributo específico (join com regras_classificacao, origem Datafarm).
    Equivalente direto ao DATA.samples do protótipo HTML, mas vindo do banco.
    """
    with get_cursor() as cur:
        if atributo:
            cur.execute("""
                SELECT a.id, a.numero_ponto,
                       ST_X(a.geom) AS lon, ST_Y(a.geom) AS lat,
                       al.valor, al.unidade, rc.classe
                FROM amostras a
                LEFT JOIN analises_laboratoriais al
                    ON al.amostra_id = a.id AND al.atributo = %s
                LEFT JOIN mapa_atributos ma ON ma.atributo_laudo = %s
                LEFT JOIN regras_classificacao rc
                    ON rc.origem = 'Datafarm' AND rc.atributo = ma.atributo_classificacao
                    AND al.valor * COALESCE(ma.fator,1.0) >= COALESCE(rc.limite_inf, -1e9)
                    AND al.valor * COALESCE(ma.fator,1.0) <  COALESCE(rc.limite_sup, 1e9)
                WHERE a.talhao_id = %s
                ORDER BY a.numero_ponto::int;
            """, (atributo, atributo, talhao_id))
        else:
            cur.execute("""
                SELECT a.id, a.numero_ponto, ST_X(a.geom) AS lon, ST_Y(a.geom) AS lat
                FROM amostras a WHERE a.talhao_id = %s ORDER BY a.numero_ponto::int;
            """, (talhao_id,))
        return cur.fetchall()


@app.get("/talhoes/{talhao_id}/amostras/{amostra_id}")
def detalhe_amostra(talhao_id: str, amostra_id: str):
    """Todos os atributos de uma amostra específica — usado no popup de clique no mapa."""
    with get_cursor() as cur:
        cur.execute("""
            SELECT al.atributo, al.valor, al.unidade
            FROM analises_laboratoriais al
            WHERE al.amostra_id = %s
            ORDER BY al.atributo;
        """, (amostra_id,))
        resultados = cur.fetchall()
        if not resultados:
            raise HTTPException(404, "Amostra não encontrada ou sem análises")
        return {"amostra_id": amostra_id, "resultados": resultados}


def _resolve_safra_id(cur, talhao_id: str, safra_id: str | None):
    """Se safra_id não for informado, usa a safra mais recente do talhão
    (por nome, ex.: '2025/26' > '2024/25'), ou None se não houver nenhuma
    (caso de talhões antigos importados antes do conceito de safra existir)."""
    if safra_id:
        return safra_id
    cur.execute("SELECT id FROM safras WHERE talhao_id = %s ORDER BY nome DESC LIMIT 1;", (talhao_id,))
    row = cur.fetchone()
    return row["id"] if row else None


@app.get("/talhoes/{talhao_id}/app")
def dados_completos_app(talhao_id: str, safra_id: str | None = None):
    """
    Endpoint único que devolve tudo que o app-cliente (HTML) precisa numa
    chamada só: talhão, amostras com TODOS os atributos já classificados,
    e a geometria das zonas de Voronoi (calculada ao vivo pelo PostGIS via
    ST_VoronoiPolygons) já normalizada para desenho em SVG.
    Substitui os objetos DATA e GEO que antes estavam congelados no HTML.
    Se safra_id não for informado, usa a safra mais recente cadastrada
    (ou todas as amostras sem safra, em talhões antigos sem esse campo).
    """
    with get_cursor() as cur:
        # --- talhão ---
        cur.execute("""
            SELECT t.id, t.nome, t.area_ha, f.nome AS fazenda, p.nome AS propriedade,
                   ST_AsGeoJSON(t.geom)::json AS geometria
            FROM talhoes t
            JOIN fazendas f ON f.id = t.fazenda_id
            JOIN propriedades p ON p.id = f.propriedade_id
            WHERE t.id = %s;
        """, (talhao_id,))
        talhao = cur.fetchone()
        if not talhao:
            raise HTTPException(404, "Talhão não encontrado")

        safra_id = _resolve_safra_id(cur, talhao_id, safra_id)
        safra_filtro = "AND a.safra_id = %s" if safra_id else ""
        params_amostras = (talhao_id, safra_id) if safra_id else (talhao_id,)

        # --- amostras + todos os atributos já classificados (join com mapa_atributos/regras) ---
        cur.execute(f"""
            SELECT a.id, a.numero_ponto, ST_X(a.geom) AS lon, ST_Y(a.geom) AS lat,
                   al.atributo, al.valor, al.unidade, rc.classe
            FROM amostras a
            JOIN analises_laboratoriais al ON al.amostra_id = a.id
            LEFT JOIN mapa_atributos ma ON ma.atributo_laudo = al.atributo
            LEFT JOIN regras_classificacao rc
                ON rc.origem = 'Datafarm' AND rc.atributo = ma.atributo_classificacao
                AND al.valor * COALESCE(ma.fator,1.0) >= COALESCE(rc.limite_inf, -1e9)
                AND al.valor * COALESCE(ma.fator,1.0) <  COALESCE(rc.limite_sup, 1e9)
            WHERE a.talhao_id = %s {safra_filtro}
            ORDER BY a.numero_ponto::int, al.atributo;
        """, params_amostras)
        rows = cur.fetchall()

        # --- zonas de Voronoi + área real (ha), calculadas no PostGIS ---
        cur.execute(f"""
            WITH pts AS (
                SELECT a.id AS amostra_id, a.numero_ponto, a.geom
                FROM amostras a WHERE a.talhao_id = %s {safra_filtro}
            ),
            mp AS ( SELECT ST_Collect(geom) AS g FROM pts ),
            vor AS ( SELECT (ST_Dump(ST_VoronoiPolygons(g))).geom AS cell FROM mp )
            SELECT p.amostra_id, p.numero_ponto,
                   ST_AsGeoJSON(ST_Intersection(v.cell, t.geom))::json AS zona,
                   ST_Area(ST_Intersection(v.cell, t.geom)::geography)/10000.0 AS area_ha
            FROM vor v
            JOIN talhoes t ON t.id = %s
            JOIN pts p ON ST_Contains(v.cell, p.geom)
            ORDER BY p.numero_ponto::int;
        """, params_amostras + (talhao_id,))
        zonas_raw = cur.fetchall()
        cur.execute(f"""
            SELECT DISTINCT al.atributo, al.unidade
            FROM analises_laboratoriais al
            JOIN amostras a ON a.id = al.amostra_id
            WHERE a.talhao_id = %s {safra_filtro};
        """, params_amostras)
        elements_meta_rows = cur.fetchall()

    # ---- montar "samples" (agrupando por amostra) ----
    samples_by_id = {}
    for r in rows:
        sid = r["numero_ponto"]
        if sid not in samples_by_id:
            samples_by_id[sid] = {"id": sid, "lat": r["lat"], "lon": r["lon"], "values": {}}
        samples_by_id[sid]["values"][r["atributo"]] = {
            "v": round(float(r["valor"]), 3), "classe": r["classe"]
        }
    for z in zonas_raw:
        if z["numero_ponto"] in samples_by_id:
            samples_by_id[z["numero_ponto"]]["area_ha"] = round(float(z["area_ha"]), 3)
    samples = [samples_by_id[k] for k in sorted(samples_by_id, key=lambda x: int(x))]

    elements_meta = {r["atributo"]: {"unidade": r["unidade"]} for r in elements_meta_rows}

    # ---- normalizar geometrias (lon/lat) para um viewBox SVG, igual ao protótipo ----
    field_coords = talhao["geometria"]["coordinates"][0]
    lons = [c[0] for c in field_coords]
    lats = [c[1] for c in field_coords]
    minx, maxx, miny, maxy = min(lons), max(lons), min(lats), max(lats)
    W = 1000.0
    H = W * (maxy - miny) / (maxx - minx)

    def norm(lon, lat):
        nx = (lon - minx) / (maxx - minx) * W
        ny = H - (lat - miny) / (maxy - miny) * H
        return [round(nx, 2), round(ny, 2)]

    field_norm = [norm(*c) for c in field_coords]
    zones_norm = []
    points_norm = []
    for s in samples:
        points_norm.append(norm(s["lon"], s["lat"]))
        z = next((z for z in zonas_raw if z["numero_ponto"] == s["id"]), None)
        ring = None
        if z and z["zona"] and z["zona"].get("coordinates"):
            geom = z["zona"]
            try:
                if geom["type"] == "Polygon" and geom["coordinates"] and len(geom["coordinates"][0]) >= 3:
                    ring = geom["coordinates"][0]
                elif geom["type"] == "MultiPolygon" and geom["coordinates"]:
                    # zona fragmentada (ex.: franja estreita do talhão) -> usa o fragmento de maior área
                    validos = [poly for poly in geom["coordinates"] if poly and len(poly[0]) >= 3]
                    if validos:
                        biggest = max(validos, key=lambda poly: shp_shape({"type":"Polygon","coordinates":poly}).area)
                        ring = biggest[0]
            except (KeyError, IndexError, TypeError):
                ring = None  # geometria degenerada (poucas amostras/pontos colineares) -> zona não desenhada, mas API não quebra
        zones_norm.append([norm(*c) for c in ring] if ring else None)

    return {
        "talhao": {
            "nome": talhao["nome"], "fazenda": talhao["fazenda"], "propriedade": talhao["propriedade"],
            "area_ha": round(float(talhao["area_ha"]), 2), "n_amostras": len(samples),
        },
        "samples": samples,
        "elements_meta": elements_meta,
        "geo": {
            "viewBox": [0, 0, round(W, 1), round(H, 1)],
            "field": field_norm,
            "zones": zones_norm,
            "points": points_norm,
        },
    }


@app.post("/talhoes/{talhao_id}/recomendacao/calagem")
def recomendar_calagem(talhao_id: str, v2: float = 70, prnt: float = 90, prof: float = 20, safra_id: str | None = None):
    """
    Método da saturação por bases — mesma fórmula do protótipo, mas calculada
    a partir dos dados reais no banco (V1 e CTC de cada amostra do talhão).
    """
    with get_cursor() as cur:
        safra_id = _resolve_safra_id(cur, talhao_id, safra_id)
        filtro = "AND a.safra_id = %s" if safra_id else ""
        params = (talhao_id, safra_id) if safra_id else (talhao_id,)
        cur.execute(f"""
            SELECT a.id, a.numero_ponto,
                   MAX(CASE WHEN al.atributo = 'Saturação por bases' THEN al.valor END) AS v1,
                   MAX(CASE WHEN al.atributo = 'Capac. de troca de cátions' THEN al.valor END) AS ctc
            FROM amostras a
            JOIN analises_laboratoriais al ON al.amostra_id = a.id
            WHERE a.talhao_id = %s {filtro}
            GROUP BY a.id, a.numero_ponto
            ORDER BY a.numero_ponto::int;
        """, params)
        rows = cur.fetchall()

    result = []
    for r in rows:
        if r["v1"] is None or r["ctc"] is None:
            result.append({**r, "dose_t_ha": None})
            continue
        nc = (v2 - float(r["v1"])) * float(r["ctc"]) / 100
        nc = max(0, nc) * (100 / prnt) * (prof / 20)
        result.append({**r, "dose_t_ha": round(nc, 3)})
    return {"parametros": {"v2": v2, "prnt": prnt, "prof": prof}, "amostras": result}


# ============================================================
# Exportação Shapefile — formato aceito por monitores agrícolas reais
# ============================================================
from pydantic import BaseModel
import shapefile as pyshp
import io, zipfile

class DoseZona(BaseModel):
    numero_ponto: str
    dose: float

class ExportShapefileBody(BaseModel):
    safra_id: str | None = None
    fonte: str = "prescricao"
    unidade: str = "kg/ha"
    doses: list[DoseZona]

# WKT padrão do GCS WGS84 — necessário pro .prj, senão o GIS/monitor não sabe
# em qual sistema de coordenadas o arquivo está.
WKT_WGS84 = (
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
)

def _zonas_reais_wgs84(cur, talhao_id: str, safra_id: str | None):
    """Recalcula as zonas de Voronoi em coordenadas geográficas reais (lon/lat),
    sem normalização — usadas só para exportação, não para desenho em tela."""
    filtro = "AND a.safra_id = %s" if safra_id else ""
    params = (talhao_id, safra_id) if safra_id else (talhao_id,)
    cur.execute(f"""
        WITH pts AS (
            SELECT a.id AS amostra_id, a.numero_ponto, a.geom
            FROM amostras a WHERE a.talhao_id = %s {filtro}
        ),
        mp AS ( SELECT ST_Collect(geom) AS g FROM pts ),
        vor AS ( SELECT (ST_Dump(ST_VoronoiPolygons(g))).geom AS cell FROM mp )
        SELECT p.numero_ponto,
               ST_AsGeoJSON(ST_Intersection(v.cell, t.geom))::json AS zona
        FROM vor v
        JOIN talhoes t ON t.id = %s
        JOIN pts p ON ST_Contains(v.cell, p.geom)
        ORDER BY p.numero_ponto::int;
    """, params + (talhao_id,))
    return cur.fetchall()

def _maior_anel(geom):
    """Extrai o anel externo de um Polygon, ou o maior fragmento de um MultiPolygon
    (mesma lógica de robustez usada no endpoint /app)."""
    if not geom or not geom.get("coordinates"):
        return None
    try:
        if geom["type"] == "Polygon" and len(geom["coordinates"][0]) >= 3:
            return geom["coordinates"][0]
        if geom["type"] == "MultiPolygon":
            validos = [poly for poly in geom["coordinates"] if poly and len(poly[0]) >= 3]
            if validos:
                biggest = max(validos, key=lambda poly: shp_shape({"type": "Polygon", "coordinates": poly}).area)
                return biggest[0]
    except (KeyError, IndexError, TypeError):
        return None
    return None


@app.post("/talhoes/{talhao_id}/exportacao/shapefile")
def exportar_shapefile(talhao_id: str, body: ExportShapefileBody):
    """
    Gera um Shapefile de prescrição (zonas de aplicação em polígono + dose)
    a partir das doses já calculadas no app (uma por amostra/zona), prontas
    pra importar em QGIS ou em monitores agrícolas que aceitam .shp.
    """
    dose_map = {d.numero_ponto: d.dose for d in body.doses}

    with get_cursor() as cur:
        cur.execute("SELECT nome FROM talhoes WHERE id = %s;", (talhao_id,))
        trow = cur.fetchone()
        if not trow:
            raise HTTPException(404, "Talhão não encontrado")
        zonas = _zonas_reais_wgs84(cur, talhao_id, body.safra_id)

    if not zonas:
        raise HTTPException(400, "Nenhuma zona encontrada para este talhão/safra.")

    shp_buf, shx_buf, dbf_buf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    writer = pyshp.Writer(shp=shp_buf, shx=shx_buf, dbf=dbf_buf, shapeType=pyshp.POLYGON)
    writer.field("zona_id", "C", size=10)
    writer.field("dose", "N", decimal=2)
    writer.field("unidade", "C", size=10)
    writer.field("talhao", "C", size=60)
    writer.field("fonte", "C", size=30)

    n_gravadas = 0
    for z in zonas:
        anel = _maior_anel(z["zona"])
        if anel is None:
            continue
        dose = dose_map.get(z["numero_ponto"])
        if dose is None:
            continue
        # shapefile exige anel no sentido horário para polígonos externos
        writer.poly([anel])
        writer.record(
            zona_id=z["numero_ponto"], dose=round(float(dose), 2),
            unidade=body.unidade, talhao=trow["nome"], fonte=body.fonte,
        )
        n_gravadas += 1
    writer.close()

    if n_gravadas == 0:
        raise HTTPException(400, "Nenhuma zona com dose correspondente foi encontrada — confira o safra_id e os números de ponto enviados.")

    zip_buf = io.BytesIO()
    base = f"prescricao_{body.fonte}"
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{base}.shp", shp_buf.getvalue())
        zf.writestr(f"{base}.shx", shx_buf.getvalue())
        zf.writestr(f"{base}.dbf", dbf_buf.getvalue())
        zf.writestr(f"{base}.prj", WKT_WGS84)
    zip_buf.seek(0)

    from fastapi.responses import Response
    return Response(
        content=zip_buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{base}.zip"'},
    )
