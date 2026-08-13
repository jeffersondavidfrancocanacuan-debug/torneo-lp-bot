import discord
from discord import app_commands
from discord.ext import tasks
import requests
import json
import os
import random
import datetime
from threading import Thread
from flask import Flask, jsonify, render_template_string

# ================= CONFIGURACIÓN =================
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
RIOT_API_KEY = os.environ.get('RIOT_API_KEY')
CANAL_CLASIFICACION_ID = int(os.environ.get('CANAL_CLASIFICACION_ID', '0'))
DURACION_TORNEO = int(os.environ.get('DURACION_TORNEO', '30'))
JUEGOS_MINIMOS_CUENTA = int(os.environ.get('JUEGOS_MINIMOS_CUENTA', '15'))
VOZ_MINIMA_MINUTOS = float(os.environ.get('VOZ_MINIMA_MINUTOS', '1'))
FECHA_INICIO_TORNEO = os.environ.get('FECHA_INICIO_TORNEO', '2026-08-14T00:00:00')
PREMIO_GANADOR_USD = os.environ.get('PREMIO_GANADOR_USD', '100')
DROP_DIARIO_ACTIVO_DEFAULT = os.environ.get('DROP_DIARIO_ACTIVO', 'false').strip().lower() in ('true', '1', 'si', 'sí')
# =================================================

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


PLATFORM_MAP = {
    'lan': 'la1', 'na': 'na1', 'las': 'la2', 'euw': 'euw1', 'eune': 'eun1',
    'br': 'br1', 'tr': 'tr1', 'ru': 'ru', 'oce': 'oc1', 'jp': 'jp1', 'kr': 'kr'
}
REGION_MAP = {
    'lan': 'americas', 'na': 'americas', 'las': 'americas', 'euw': 'europe',
    'eune': 'europe', 'br': 'americas', 'tr': 'europe', 'ru': 'europe',
    'oce': 'americas', 'jp': 'asia', 'kr': 'asia'
}

ROL_LIDER_LOW = 'Lider Low Elo'
ROL_LIDER_HIGH = 'Lider High Elo'
ROL_DIRECTIVA_NOMBRE = 'Directiva'


def es_directiva(member):
    """True si el miembro es Administrador de Discord o tiene el rol 'Directiva'."""
    try:
        if member.guild_permissions.administrator:
            return True
    except Exception:
        pass
    return any(r.name.lower() == ROL_DIRECTIVA_NOMBRE.lower() for r in getattr(member, 'roles', []))


async def requiere_directiva(interaction: discord.Interaction) -> bool:
    """Verifica permisos de directiva; responde y retorna False si no las tiene."""
    if not es_directiva(interaction.user):
        await interaction.response.send_message(
            f'Solo miembros con el rol **{ROL_DIRECTIVA_NOMBRE}** (o Administrador) pueden usar este comando.',
            ephemeral=True)
        return False
    return True


LOGROS = {
    'lp_50':   {'nombre': '+50 LP',  'desc': 'Alcanzo 50 puntos netos'},
    'lp_100':  {'nombre': '+100 LP', 'desc': 'Alcanzo 100 puntos netos'},
    'lp_200':  {'nombre': '+200 LP', 'desc': 'Alcanzo 200 puntos netos'},
    'ascenso': {'nombre': 'Ascenso', 'desc': 'Subio de division desde el registro'},
    'top1':    {'nombre': 'Cima',    'desc': 'Llego al puesto #1 de su categoria'},
    'top3':    {'nombre': 'Podio',   'desc': 'Entro al top 3 de su categoria'},
}

# ------------------- ESCUDOS AZULES (maldiciones estilo Blue Shell) -------------------
MALDICION_MAX_ACTIVAS = 3
MALDICION_DURACION_HORAS = 24
ESCUDOS_MAX_INVENTARIO = 3          # maximo de Escudos Azules que un jugador puede acumular
CASTIGOS_PENDIENTES_PARA_AEGIS = 3  # castigos SIN cumplir recibidos que activan el Aegis
AEGIS_DURACION_HORAS = 24           # proteccion temporal tras activar el Aegis
POSTPARTIDA_GRACIA_MINUTOS = 10     # minutos tras terminar una partida en los que no se puede lanzar
TORNEO_BLOQUEO_FINAL_HORAS = 48     # ultimas horas del torneo en las que el sistema Blue Shell se desactiva
DROP_DIARIO_INTERVALO_MIN = 10      # frecuencia de revision de partidas para escudos automaticos


def cooldown_recepcion_horas(posicion):
    """Cooldown (horas) antes de poder volver a maldecir a alguien, segun su posicion en la tabla.
    Top1: sin cooldown. Top2: 4h. Top3-5: 6h. Resto (o sin clasificar): 12h."""
    if posicion == 1:
        return 0
    if posicion == 2:
        return 4
    if posicion in (3, 4, 5):
        return 6
    return 12


def probabilidad_reverse(posicion):
    """Probabilidad de que la maldicion rebote hacia quien la lanzo, segun la posicion del objetivo.
    Cuanto mas abajo este el objetivo, mas facil es que rebote (tirar hacia arriba es mas seguro)."""
    tabla = {1: 0.01, 2: 0.02, 3: 0.03, 4: 0.04, 5: 0.05}
    return tabla.get(posicion, 0.15)


def posicion_de_jugador(db, puuid):
    """Posicion (1-indexed) del jugador en su categoria (High/Low Elo) segun la tabla actual.
    Devuelve None si no aparece en ninguna tabla (pendiente, sin voz, etc.) -> se trata como 'resto'."""
    high, low, _, _ = calcular_tabla(db)
    for lista in (high, low):
        for i, j in enumerate(lista, 1):
            if j['puuid'] == puuid:
                return i
    return None

DDRAGON_VERSION = '14.23.1'
CAMPEONES_POOL = [
    'Teemo', 'Yuumi', 'Singed', 'Nunu', 'Amumu', 'Shaco', 'Fiddlesticks',
    'Urgot', 'Heimerdinger', 'Ziggs', 'Corki', 'Kled', 'Rammus', 'Zilean',
    'Yorick', 'Illaoi', 'Cho\'Gath', 'Nasus', 'Veigar', 'Anivia',
]


def icono_campeon(nombre):
    n = nombre.replace("'", "").replace(" ", "")
    return f'https://ddragon.leagueoflegends.com/cdn/{DDRAGON_VERSION}/img/champion/{n}.png'


def generar_efecto_maldicion(posicion_objetivo=None):
    """Devuelve un dict {tipo, texto, opciones} representando el efecto de la maldicion.
    Primero se sortea el Reverse segun la posicion del objetivo; si no sale, se sortea un efecto normal."""
    if random.random() < probabilidad_reverse(posicion_objetivo):
        return {
            'tipo': 'reverse',
            'texto': 'REVERSE: la maldicion rebota. El castigo lo cumple quien la lanzo, no el objetivo.',
            'opciones': [], 'elegido': None,
        }
    tipo = random.choice(['hechizos', 'campeon', 'rol', 'baneo'])
    if tipo == 'campeon':
        opciones = random.sample(CAMPEONES_POOL, 3)
        return {
            'tipo': 'campeon',
            'texto': 'Debes elegir y jugar uno de estos 3 campeones en tu proxima partida (usa /elegir_campeon).',
            'opciones': opciones,
            'elegido': None,
        }
    textos = {
        'hechizos': 'Hechizos de invocador obligatorios: solo Flash + Ignite en tu proxima partida.',
        'rol': 'Rol/posicion aleatoria obligatoria en tu proxima partida.',
        'baneo': 'Debes banear el campeon que te pida quien te maldijo en tu proxima partida.',
    }
    return {'tipo': tipo, 'texto': textos[tipo], 'opciones': [], 'elegido': None}


# Sesiones de voz activas en memoria: {discord_id: datetime_de_ultimo_checkpoint}
VOICE_SESIONES = {}


# ------------------- PERSISTENCIA (Google Sheets) -------------------

GOOGLE_SHEET_NAME = os.environ.get('GOOGLE_SHEET_NAME', 'SoloQ Challenge DB')
GOOGLE_CREDENTIALS_JSON = os.environ.get('GOOGLE_CREDENTIALS_JSON', '')

JUGADORES_HEADERS = [
    'puuid', 'discord_id', 'nombre', 'region', 'lp_inicial', 'tier_inicial', 'rank_inicial',
    'elo', 'estado', 'fecha_registro', 'bonus_total', 'castigos_total', 'logros',
    'tiempo_voz_min', 'elo_previo', 'escudos', 'maldiciones', 'ultimo_escudo_uso', 'escudo_hasta',
    'ultima_maldicion_recibida', 'ultimo_match_procesado', 'racha_victorias',
    'campeones_ganados', 'victorias_con_castigo_contador',
]
META_HEADERS = ['clave', 'valor']
REGISTROS_HEADERS = ['tipo', 'usuario', 'nombre', 'puntos', 'motivo', 'fecha']

_gs_client = None
_gs_spreadsheet = None


def _get_spreadsheet():
    global _gs_client, _gs_spreadsheet
    if _gs_spreadsheet is not None:
        return _gs_spreadsheet
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    _gs_client = gspread.authorize(creds)
    _gs_spreadsheet = _gs_client.open(GOOGLE_SHEET_NAME)
    return _gs_spreadsheet


def _get_or_create_worksheet(nombre, headers):
    import gspread
    sheet = _get_spreadsheet()
    try:
        ws = sheet.worksheet(nombre)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=nombre, rows=1000, cols=max(20, len(headers)))
        ws.update('A1', [headers], value_input_option='RAW')
    return ws


def _valor_a_texto(valor):
    if valor is None:
        return ''
    if isinstance(valor, (list, dict)):
        return json.dumps(valor, ensure_ascii=False)
    return str(valor)


def cargar_db():
    try:
        ws = _get_or_create_worksheet('jugadores', JUGADORES_HEADERS)
        filas = ws.get_all_records(numericise_ignore=['all'])
        db = {}
        for f in filas:
            puuid = f.get('puuid')
            if not puuid:
                continue
            try:
                logros = json.loads(f.get('logros') or '[]')
            except Exception:
                logros = []
            try:
                maldiciones = json.loads(f.get('maldiciones') or '[]')
            except Exception:
                maldiciones = []
            # compatibilidad con maldiciones antiguas (sin campo 'cumplido'/'tipo')
            for m in maldiciones:
                m.setdefault('cumplido', False)
                m.setdefault('tipo', 'legacy')
                m.setdefault('opciones', [])
                m.setdefault('elegido', None)
            try:
                campeones_ganados = json.loads(f.get('campeones_ganados') or '{}')
            except Exception:
                campeones_ganados = {}
            db[puuid] = {
                'discord_id': f.get('discord_id', ''),
                'nombre': f.get('nombre', ''),
                'region': f.get('region', ''),
                'lp_inicial': int(float(f.get('lp_inicial') or 0)),
                'tier_inicial': f.get('tier_inicial', ''),
                'rank_inicial': f.get('rank_inicial', ''),
                'elo': f.get('elo') or '',
                'estado': f.get('estado') or 'pendiente',
                'fecha_registro': f.get('fecha_registro', ''),
                'bonus_total': int(float(f.get('bonus_total') or 0)),
                'castigos_total': int(float(f.get('castigos_total') or 0)),
                'logros': logros,
                'tiempo_voz_min': float(f.get('tiempo_voz_min') or 0),
                'elo_previo': f.get('elo_previo', ''),
                'escudos': int(float(f.get('escudos') or 0)),
                'maldiciones': maldiciones,
                'ultimo_escudo_uso': f.get('ultimo_escudo_uso', ''),
                'escudo_hasta': f.get('escudo_hasta', ''),
                'ultima_maldicion_recibida': f.get('ultima_maldicion_recibida', ''),
                'ultimo_match_procesado': f.get('ultimo_match_procesado', ''),
                'racha_victorias': int(float(f.get('racha_victorias') or 0)),
                'campeones_ganados': campeones_ganados,
                'victorias_con_castigo_contador': int(float(f.get('victorias_con_castigo_contador') or 0)),
            }
        meta_ws = _get_or_create_worksheet('meta', META_HEADERS)
        for fila in meta_ws.get_all_records(numericise_ignore=['all']):
            clave = fila.get('clave')
            valor = fila.get('valor')
            if clave == 'inicio_torneo' and valor:
                db['inicio_torneo'] = valor
            elif clave == 'torneo_iniciado':
                db['torneo_iniciado'] = str(valor).strip().lower() in ('true', '1', 'si', 'sí')
            elif clave == 'drop_diario_activo':
                db['drop_diario_activo'] = str(valor).strip().lower() in ('true', '1', 'si', 'sí')
            elif clave == 'reto_activo_texto':
                db['reto_activo_texto'] = valor
            elif clave == 'reto_activo_fecha':
                db['reto_activo_fecha'] = valor
        if 'drop_diario_activo' not in db:
            db['drop_diario_activo'] = DROP_DIARIO_ACTIVO_DEFAULT
        return db
    except Exception as e:
        print(f'Error cargando DB desde Google Sheets: {e}')
        return {}


def guardar_db(db):
    try:
        ws = _get_or_create_worksheet('jugadores', JUGADORES_HEADERS)
        filas = [JUGADORES_HEADERS]
        for puuid, data in jugadores_validos(db).items():
            fila = [puuid] + [_valor_a_texto(data.get(campo, '')) for campo in JUGADORES_HEADERS[1:]]
            filas.append(fila)
        ws.clear()
        ws.update('A1', filas, value_input_option='RAW')

        meta_ws = _get_or_create_worksheet('meta', META_HEADERS)
        meta_filas = [META_HEADERS,
                      ['inicio_torneo', _valor_a_texto(db.get('inicio_torneo', ''))],
                      ['torneo_iniciado', 'True' if db.get('torneo_iniciado') else 'False'],
                      ['drop_diario_activo', 'True' if db.get('drop_diario_activo') else 'False'],
                      ['reto_activo_texto', _valor_a_texto(db.get('reto_activo_texto', ''))],
                      ['reto_activo_fecha', _valor_a_texto(db.get('reto_activo_fecha', ''))]]
        meta_ws.clear()
        meta_ws.update('A1', meta_filas, value_input_option='RAW')
    except Exception as e:
        print(f'Error guardando DB en Google Sheets: {e}')


def cargar_registros():
    try:
        ws = _get_or_create_worksheet('registros', REGISTROS_HEADERS)
        filas = ws.get_all_records(numericise_ignore=['all'])
        registros = []
        for f in filas:
            if not f.get('usuario'):
                continue
            registros.append({
                'tipo': f.get('tipo', ''),
                'usuario': f.get('usuario', ''),
                'nombre': f.get('nombre', ''),
                'puntos': int(float(f.get('puntos') or 0)),
                'motivo': f.get('motivo', ''),
                'fecha': f.get('fecha', ''),
            })
        return registros
    except Exception as e:
        print(f'Error cargando registros desde Google Sheets: {e}')
        return []


def guardar_registros(registros):
    try:
        ws = _get_or_create_worksheet('registros', REGISTROS_HEADERS)
        filas = [REGISTROS_HEADERS]
        for r in registros:
            filas.append([_valor_a_texto(r.get(campo, '')) for campo in REGISTROS_HEADERS])
        ws.clear()
        ws.update('A1', filas, value_input_option='RAW')
    except Exception as e:
        print(f'Error guardando registros en Google Sheets: {e}')


META_KEYS = ('inicio_torneo', 'torneo_iniciado', 'drop_diario_activo', 'reto_activo_texto', 'reto_activo_fecha')


def jugadores_validos(db):
    return {k: v for k, v in db.items() if k not in META_KEYS and isinstance(v, dict)}


# ------------------- VOZ -------------------

def flush_voice_time(discord_id):
    """Suma el tiempo transcurrido desde el ultimo checkpoint al total del jugador y reinicia el checkpoint."""
    inicio = VOICE_SESIONES.get(discord_id)
    if inicio is None:
        return
    ahora = datetime.datetime.now()
    minutos = (ahora - inicio).total_seconds() / 60
    VOICE_SESIONES[discord_id] = ahora
    if minutos <= 0:
        return
    db = cargar_db()
    cambiado = False
    for puuid, data in jugadores_validos(db).items():
        if data['discord_id'] == discord_id:
            data['tiempo_voz_min'] = data.get('tiempo_voz_min', 0) + minutos
            cambiado = True
            break
    if cambiado:
        guardar_db(db)


# ------------------- ESCUDOS AZULES: helpers -------------------

def maldiciones_activas_de(data):
    ahora = datetime.datetime.now()
    activas = []
    for m in data.get('maldiciones', []):
        try:
            fecha = datetime.datetime.fromisoformat(m['fecha'])
        except Exception:
            continue
        if (ahora - fecha).total_seconds() / 3600 < MALDICION_DURACION_HORAS:
            activas.append(m)
    return activas


def castigos_pendientes_de(data):
    """Maldiciones activas y sin marcar como 'cumplido' por la directiva."""
    return [m for m in maldiciones_activas_de(data) if not m.get('cumplido')]


def aegis_activo(data):
    hasta = data.get('escudo_hasta')
    if not hasta:
        return False
    try:
        fecha = datetime.datetime.fromisoformat(hasta)
    except Exception:
        return False
    return datetime.datetime.now() < fecha


def aegis_restante_horas(data):
    hasta = data.get('escudo_hasta')
    if not hasta:
        return 0
    try:
        fecha = datetime.datetime.fromisoformat(hasta)
    except Exception:
        return 0
    return max((fecha - datetime.datetime.now()).total_seconds() / 3600, 0)


# ------------------- RIOT API -------------------

def obtener_info_ranked(riot_id, region):
    """riot_id con formato 'Nombre#TAG'. Usa account-v1 + league-v4 by-puuid."""
    plataforma = PLATFORM_MAP.get(region.lower())
    region_base = REGION_MAP.get(region.lower())
    if not plataforma or not region_base or '#' not in riot_id:
        return None
    game_name, tag_line = riot_id.split('#', 1)
    headers = {'X-Riot-Token': RIOT_API_KEY}

    url = f'https://{region_base}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}'
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        return None
    cuenta = r.json()
    puuid = cuenta['puuid']
    nombre_completo = f"{cuenta['gameName']}#{cuenta['tagLine']}"

    url2 = f'https://{plataforma}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}'
    r2 = requests.get(url2, headers=headers)
    if r2.status_code != 200:
        return None
    for entry in r2.json():
        if entry['queueType'] == 'RANKED_SOLO_5x5':
            return {
                'puuid': puuid, 'tier': entry['tier'], 'rank': entry['rank'],
                'lp': entry['leaguePoints'], 'wins': entry['wins'], 'losses': entry['losses'],
                'nombre': nombre_completo
            }
    return {
        'puuid': puuid, 'tier': 'UNRANKED', 'rank': '', 'lp': 0, 'wins': 0, 'losses': 0,
        'nombre': nombre_completo
    }


def esta_en_partida_activa(puuid, plataforma):
    """Spectator API: True si el jugador tiene una partida en vivo en este momento.
    Nota: la API publica de Riot no expone cola/seleccion de campeon, solo partidas ya iniciadas."""
    try:
        headers = {'X-Riot-Token': RIOT_API_KEY}
        url = f'https://{plataforma}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}'
        r = requests.get(url, headers=headers, timeout=6)
        return r.status_code == 200
    except Exception:
        return False


def en_ventana_postpartida(puuid, region_base):
    """True si la ULTIMA PARTIDA RANKED SOLO/DUO (queueId 420) del jugador termino hace menos de
    POSTPARTIDA_GRACIA_MINUTOS. Ignora normales, ARAM, personalizadas y remakes (<5 min), que no
    afectan al torneo y no deben bloquear el lanzamiento de maldiciones."""
    try:
        headers = {'X-Riot-Token': RIOT_API_KEY}
        # Se piden varias partidas recientes (no solo la ultima) porque la ultima jugada puede ser
        # una normal/ARAM/personalizada posterior a la ranked; se busca la ranked mas reciente.
        url = f'https://{region_base}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count=5'
        r = requests.get(url, headers=headers, timeout=6)
        if r.status_code != 200 or not r.json():
            return False
        for match_id in r.json():
            url2 = f'https://{region_base}.api.riotgames.com/lol/match/v5/matches/{match_id}'
            r2 = requests.get(url2, headers=headers, timeout=6)
            if r2.status_code != 200:
                continue
            info = r2.json()['info']
            fin_ms = info.get('gameEndTimestamp')
            if not fin_ms:
                continue
            fin = datetime.datetime.fromtimestamp(fin_ms / 1000)
            segundos_desde_fin = (datetime.datetime.now() - fin).total_seconds()
            # Si ya paso la ventana de gracia incluso para la partida mas reciente, no hace falta
            # seguir revisando partidas mas antiguas (estan aun mas lejos en el tiempo).
            if segundos_desde_fin >= POSTPARTIDA_GRACIA_MINUTOS * 60:
                return False
            if info.get('queueId') != 420 or info.get('gameDuration', 0) < 300:
                continue  # no es ranked solo/duo valida (o fue un remake) -> no cuenta, se sigue buscando
            return True
        return False
    except Exception:
        return False


def motivo_bloqueo_por_partida(puuid, data):
    """Devuelve un texto de motivo si el jugador no puede lanzar/recibir por su estado de partida, o None si puede."""
    plataforma = PLATFORM_MAP.get(data.get('region', 'lan').lower())
    region_base = REGION_MAP.get(data.get('region', 'lan').lower())
    if not plataforma or not region_base:
        return None
    try:
        if esta_en_partida_activa(puuid, plataforma):
            return 'esta en una partida en vivo ahora mismo'
        if en_ventana_postpartida(puuid, region_base):
            return f'termino una partida hace menos de {POSTPARTIDA_GRACIA_MINUTOS} minutos (se esta procesando el resultado)'
    except Exception:
        return None
    return None


def torneo_en_ultimas_48h(db):
    """True si quedan menos de TORNEO_BLOQUEO_FINAL_HORAS para el cierre del torneo (Blue Shell desactivado)."""
    if not db.get('torneo_iniciado'):
        return False
    try:
        inicio = datetime.datetime.fromisoformat(db.get('inicio_torneo'))
    except Exception:
        return False
    fin = inicio + datetime.timedelta(days=DURACION_TORNEO)
    horas_restantes = (fin - datetime.datetime.now()).total_seconds() / 3600
    return horas_restantes <= TORNEO_BLOQUEO_FINAL_HORAS


TIER_ORDEN = ['IRON', 'BRONZE', 'SILVER', 'GOLD', 'PLATINUM', 'EMERALD',
              'DIAMOND', 'MASTER', 'GRANDMASTER', 'CHALLENGER']
RANK_ORDEN = ['IV', 'III', 'II', 'I']  # de menor a mayor dentro de una division


def tier_index(tier):
    try:
        return TIER_ORDEN.index(tier.upper())
    except ValueError:
        return -1


def rank_index(rank):
    try:
        return RANK_ORDEN.index(rank.upper())
    except ValueError:
        return 0


def valor_escalado(tier, rank, lp):
    """Metrica de progreso basada en escalar de division/liga, no solo LP crudo.
    Cada division vale 400 puntos, cada sub-rango (IV-I) vale 100 puntos, mas los LP dentro de ese rango.
    A partir de Master+ (sin sub-rangos) se usa el LP directo sumado al piso de Master."""
    ti = max(tier_index(tier), 0)
    if tier.upper() in ('MASTER', 'GRANDMASTER', 'CHALLENGER'):
        piso = tier_index('MASTER') * 400
        return piso + lp
    ri = rank_index(rank)
    return ti * 400 + ri * 100 + min(lp, 100)


# ------------------- ESTADO DEL TORNEO -------------------

def calcular_estado_torneo(db):
    ahora = datetime.datetime.now()
    try:
        fecha_inicio_oficial = datetime.datetime.fromisoformat(FECHA_INICIO_TORNEO)
    except Exception:
        fecha_inicio_oficial = ahora
    if db.get('torneo_iniciado'):
        try:
            inicio = datetime.datetime.fromisoformat(db.get('inicio_torneo'))
        except Exception:
            inicio = ahora
        fin = inicio + datetime.timedelta(days=DURACION_TORNEO)
        dias_restantes = max((fin - ahora).days, 0)
        return f'Torneo en curso - quedan {dias_restantes} dias para el cierre'
    if ahora < fecha_inicio_oficial:
        dias = (fecha_inicio_oficial - ahora).days
        return f'Periodo de pruebas - inicio oficial en {dias} dias ({fecha_inicio_oficial.strftime("%d/%m/%Y")})'
    return 'Esperando que la directiva use /iniciar_torneo para comenzar oficialmente'


# ------------------- CALCULO DE TABLA -------------------

def calcular_tabla(db):
    """Devuelve (high, low, pendientes, sin_voz) con los datos ya frescos de Riot.
    El orden dentro de cada categoria se basa en 'escalado' (division/liga), no solo LP crudo."""
    high, low, pendientes, sin_voz = [], [], [], []
    for puuid, data in jugadores_validos(db).items():
        info = obtener_info_ranked(data['nombre'], data['region'])
        if info is None:
            continue
        lp_ganados = info['lp'] - data['lp_inicial']
        escalado_inicial = valor_escalado(data.get('tier_inicial', info['tier']), data.get('rank_inicial', ''), data['lp_inicial'])
        escalado_actual = valor_escalado(info['tier'], info['rank'], info['lp'])
        progreso_escalado = escalado_actual - escalado_inicial
        total = progreso_escalado + data.get('bonus_total', 0) - data.get('castigos_total', 0)
        tiempo_voz = round(data.get('tiempo_voz_min', 0), 1)
        pendientes_castigo = castigos_pendientes_de(data)
        jugador = {
            'puuid': puuid,
            'discord_id': data['discord_id'],
            'nombre': data['nombre'],
            'lp_ganados': lp_ganados,
            'lp_actual': info['lp'],
            'tier_actual': info['tier'],
            'rank_actual': info['rank'],
            'tier_inicial': data.get('tier_inicial', info['tier']),
            'escalado': progreso_escalado,
            'elo': data.get('elo', ''),
            'bonus': data.get('bonus_total', 0),
            'castigos': data.get('castigos_total', 0),
            'total': total,
            'estado': data.get('estado', 'pendiente'),
            'tiempo_voz_min': tiempo_voz,
            'voz_verificado': tiempo_voz >= VOZ_MINIMA_MINUTOS,
            'elo_previo': data.get('elo_previo', ''),
            'escudos': data.get('escudos', 0),
            'aegis_activo': aegis_activo(data),
            'aegis_restante': round(aegis_restante_horas(data), 1),
            'castigos_pendientes': len(pendientes_castigo),
        }
        if jugador['estado'] == 'pendiente' or not jugador['elo']:
            pendientes.append(jugador)
        elif not jugador['voz_verificado']:
            sin_voz.append(jugador)
        elif jugador['elo'] == 'high':
            high.append(jugador)
        else:
            low.append(jugador)
    high.sort(key=lambda x: x['total'], reverse=True)
    low.sort(key=lambda x: x['total'], reverse=True)
    return high, low, pendientes, sin_voz


# ------------------- LOGROS Y ROLES -------------------

async def procesar_logros_y_roles(canal, high, low, db):
    guild = canal.guild if canal else None
    anuncios = []

    for categoria, lista in (('high', high), ('low', low)):
        for pos, j in enumerate(lista, 1):
            data = db.get(j['puuid'])
            if not data:
                continue
            logros_actuales = set(data.get('logros', []))
            nuevos = set()
            if j['total'] >= 50:
                nuevos.add('lp_50')
            if j['total'] >= 100:
                nuevos.add('lp_100')
            if j['total'] >= 200:
                nuevos.add('lp_200')
            if tier_index(j['tier_actual']) > tier_index(j['tier_inicial']):
                nuevos.add('ascenso')
            if pos == 1:
                nuevos.add('top1')
            if pos <= 3:
                nuevos.add('top3')

            recien_desbloqueados = nuevos - logros_actuales
            if recien_desbloqueados:
                data['logros'] = list(logros_actuales | nuevos)
                espacio = max(ESCUDOS_MAX_INVENTARIO - data.get('escudos', 0), 0)
                otorgados = min(len(recien_desbloqueados), espacio)
                data['escudos'] = data.get('escudos', 0) + otorgados
                for clave in recien_desbloqueados:
                    info_logro = LOGROS.get(clave)
                    if info_logro:
                        extra = ' (+1 Escudo Azul)' if otorgados > 0 else ' (inventario de escudos lleno)'
                        anuncios.append(
                            f"{info_logro['nombre']} - <@{j['discord_id']}> **{j['nombre']}** {info_logro['desc']} "
                            f"({'High' if categoria == 'high' else 'Low'} Elo){extra}")

    guardar_db(db)

    if anuncios and canal:
        try:
            texto = '**Nuevos logros desbloqueados (automatico)**\n' + '\n'.join(anuncios[:10])
            await canal.send(texto)
        except Exception:
            pass

    # Roles automaticos de lider
    if guild:
        try:
            for categoria, lista, nombre_rol in (('high', high, ROL_LIDER_HIGH), ('low', low, ROL_LIDER_LOW)):
                rol = discord.utils.get(guild.roles, name=nombre_rol)
                if rol is None:
                    rol = await guild.create_role(name=nombre_rol, colour=discord.Colour.gold(),
                                                   reason='Rol automatico de lider SoloQ Challenge')
                if not lista:
                    continue
                lider_id = int(lista[0]['discord_id'])
                for miembro in list(rol.members):
                    if miembro.id != lider_id:
                        try:
                            await miembro.remove_roles(rol, reason='Ya no es lider')
                        except Exception:
                            pass
                try:
                    miembro_lider = guild.get_member(lider_id) or await guild.fetch_member(lider_id)
                    if rol not in miembro_lider.roles:
                        await miembro_lider.add_roles(rol, reason='Nuevo lider de categoria')
                except Exception:
                    pass
        except Exception:
            pass


# ------------------- COMANDOS -------------------

@tree.command(name='registrar', description='Registra tu cuenta de LoL (LAN) para el torneo')
@app_commands.describe(nombre='Tu Riot ID completo, ej: Nombre#LAN1',
                        elo_previo='Opcional: tu elo mas alto alcanzado antes (ej. Master, Diamond). Ayuda a la directiva a clasificarte.')
async def registrar(interaction: discord.Interaction, nombre: str, elo_previo: str = ""):
    await interaction.response.defer()
    user_id = str(interaction.user.id)
    db = cargar_db()
    for jugador in jugadores_validos(db).values():
        if jugador.get('discord_id') == user_id:
            await interaction.followup.send('Ya estas registrado.')
            return
    if '#' not in nombre:
        await interaction.followup.send('Usa tu Riot ID completo con formato **Nombre#TAG** (ej: Faker#LAN1).')
        return
    region = 'lan'
    info = obtener_info_ranked(nombre, region)
    if info is None:
        await interaction.followup.send('No se encontro la cuenta. Verifica el Riot ID exacto (Nombre#TAG) y que sea de LAN.')
        return

    # Todas las cuentas empiezan SIN elo asignado (unranked para el torneo) hasta que la
    # directiva las revise manualmente con /clasificar y les asigne High Elo o Low Elo.
    ahora = datetime.datetime.now()
    db[info['puuid']] = {
        'discord_id': user_id,
        'nombre': info['nombre'],
        'region': region,
        'lp_inicial': info['lp'],
        'tier_inicial': info['tier'],
        'rank_inicial': info['rank'],
        'elo': '',
        'estado': 'pendiente',
        'fecha_registro': str(ahora),
        'bonus_total': 0,
        'castigos_total': 0,
        'logros': [],
        'tiempo_voz_min': 0,
        'elo_previo': elo_previo,
        'escudos': 0,
        'maldiciones': [],
        'ultimo_escudo_uso': None,
        'escudo_hasta': '',
        'ultima_maldicion_recibida': '',
        'ultimo_match_procesado': '',
        'racha_victorias': 0,
        'campeones_ganados': {},
        'victorias_con_castigo_contador': 0,
    }
    if 'inicio_torneo' not in db:
        db['inicio_torneo'] = str(ahora)
    guardar_db(db)

    elo_previo_txt = f'\nElo previo declarado: **{elo_previo}** (la directiva lo vera al revisar tu cuenta).' if elo_previo else ''
    await interaction.followup.send(
        f'{interaction.user.mention} registrado como **{info["nombre"]}** (LAN).\n'
        f'Rango actual detectado: {info["tier"]} {info["rank"]} ({info["lp"]} LP).\n'
        f'Tu cuenta queda **pendiente de revision**: la directiva la clasificara manualmente en '
        f'**High Elo** o **Low Elo** con `/clasificar`.{elo_previo_txt}\n'
        f'Recuerda: ademas de la aprobacion, tus puntos solo son validos si te conectas al chat de voz del servidor (cualquier canal). '
        f'Usa `/reglamento` para ver las reglas completas y `/participantes` para ver quien mas esta inscrito.'
    )


@tree.command(name='progreso', description='Mira tu progreso actual')
async def progreso(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = str(interaction.user.id)
    flush_voice_time(user_id)
    db = cargar_db()
    for puuid, data in jugadores_validos(db).items():
        if data['discord_id'] == user_id:
            info = obtener_info_ranked(data['nombre'], data['region'])
            if info is None:
                await interaction.followup.send('No se pudo obtener tu informacion. Intenta mas tarde.')
                return
            progreso_escalado = valor_escalado(info['tier'], info['rank'], info['lp']) - valor_escalado(
                data.get('tier_inicial', info['tier']), data.get('rank_inicial', ''), data['lp_inicial'])
            total = progreso_escalado + data.get('bonus_total', 0) - data.get('castigos_total', 0)
            if not data.get('elo'):
                estado_txt = 'Pendiente de clasificacion manual (Directiva)'
            elif data.get('estado') == 'pendiente':
                estado_txt = 'Pendiente de revision'
            else:
                estado_txt = f'Aprobado - {"High Elo" if data["elo"] == "high" else "Low Elo"}'
            tiempo_voz = round(data.get('tiempo_voz_min', 0), 1)
            voz_txt = f'{tiempo_voz} min (verificado)' if tiempo_voz >= VOZ_MINIMA_MINUTOS else f'{tiempo_voz} min (necesitas {round(VOZ_MINIMA_MINUTOS - tiempo_voz, 1)} min mas conectado a voz para que tus puntos cuenten)'
            await interaction.followup.send(
                f'**{data["nombre"]}** ({estado_txt})\n'
                f'Rango inicial: {data["tier_inicial"]} {data["rank_inicial"]} ({data["lp_inicial"]} LP)\n'
                f'Rango actual: {info["tier"]} {info["rank"]} ({info["lp"]} LP)\n'
                f'Progreso (escalado de division/liga): {progreso_escalado} pts\n'
                f'Bonus: +{data.get("bonus_total", 0)} | Castigos: -{data.get("castigos_total", 0)}\n'
                f'Tiempo en chat de voz: {voz_txt}\n'
                f'**Total: {total} puntos**'
            )
            return
    await interaction.followup.send('No estas registrado.')


@tree.command(name='perfil', description='Muestra tu tarjeta de jugador completa')
async def perfil(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = str(interaction.user.id)
    flush_voice_time(user_id)
    db = cargar_db()
    high, low, pendientes, sin_voz = calcular_tabla(db)

    objetivo = None
    for j in high + low + pendientes + sin_voz:
        if j['discord_id'] == user_id:
            objetivo = j
            break
    if objetivo is None:
        await interaction.followup.send('No estas registrado. Usa `/registrar` primero.')
        return

    if not objetivo['elo']:
        posicion_txt = 'Pendiente de clasificacion manual por la Directiva (High/Low Elo)'
    elif objetivo['estado'] == 'pendiente':
        posicion_txt = 'En revision por la directiva (no aparece en la tabla aun)'
    elif not objetivo['voz_verificado']:
        faltante = round(VOZ_MINIMA_MINUTOS - objetivo['tiempo_voz_min'], 1)
        posicion_txt = f'Sin verificar - conectate al chat de voz {faltante} min mas para entrar a la tabla'
    else:
        lista = high if objetivo['elo'] == 'high' else low
        posicion = next((i for i, j in enumerate(lista, 1) if j['puuid'] == objetivo['puuid']), None)
        posicion_txt = f'#{posicion} de {len(lista)} en {"High" if objetivo["elo"] == "high" else "Low"} Elo'

    data = db.get(objetivo['puuid'], {})
    logros_txt = ' '.join(LOGROS[c]['nombre'] for c in data.get('logros', []) if c in LOGROS) or 'Sin logros aun'

    embed = discord.Embed(title=f'Perfil de {objetivo["nombre"]}', color=0x5865F2)
    embed.add_field(name='Categoria', value='High Elo' if objetivo['elo'] == 'high' else ('Low Elo' if objetivo['elo'] == 'low' else 'Sin asignar'), inline=True)
    embed.add_field(name='Posicion', value=posicion_txt, inline=True)
    embed.add_field(name='Rango actual', value=f'{objetivo["tier_actual"]} {objetivo["rank_actual"]}', inline=True)
    embed.add_field(name='Escalado (progreso)', value=str(objetivo['escalado']), inline=True)
    embed.add_field(name='Bonus', value=f'+{objetivo["bonus"]}', inline=True)
    embed.add_field(name='Castigos', value=f'-{objetivo["castigos"]}', inline=True)
    embed.add_field(name='Tiempo en voz', value=f'{objetivo["tiempo_voz_min"]} min', inline=True)
    embed.add_field(name='Total', value=f'**{objetivo["total"]} pts**', inline=True)
    embed.add_field(name='Escudos Azules', value=f'{objetivo.get("escudos", 0)}/{ESCUDOS_MAX_INVENTARIO}', inline=True)
    aegis_txt = f'Activo - {objetivo["aegis_restante"]}h restantes' if objetivo['aegis_activo'] else 'Inactivo'
    embed.add_field(name='Aegis (proteccion)', value=aegis_txt, inline=True)
    if objetivo.get('elo_previo'):
        embed.add_field(name='Elo previo declarado', value=objetivo['elo_previo'], inline=True)
    activas = maldiciones_activas_de(data)
    malds_txt = '\n'.join(f'- {m["texto"]}' + (' (cumplido)' if m.get('cumplido') else ' (pendiente)') for m in activas) or 'Ninguna'
    embed.add_field(name=f'Maldiciones activas ({len(activas)}/{MALDICION_MAX_ACTIVAS})', value=malds_txt, inline=False)
    embed.add_field(name='Logros', value=logros_txt, inline=False)
    await interaction.followup.send(embed=embed)


@tree.command(name='reglamento', description='Muestra el reglamento completo del torneo SoloQ Challenge')
async def reglamento(interaction: discord.Interaction):
    embed = discord.Embed(
        title='Reglamento - SoloQ Challenge',
        description=f'Torneo interno de ganancia de rango en LoL (LAN). Duracion: {DURACION_TORNEO} dias. Premio principal: ${PREMIO_GANADOR_USD} + insignias de logro.',
        color=0xf5c518,
    )
    embed.add_field(
        name='1. Registro',
        value=('Todos se registran con `/registrar nombre:Riot#TAG`. Toda cuenta inicia **sin categoria** '
               '(pendiente). La Directiva revisa y asigna manualmente **High Elo** o **Low Elo** con `/clasificar`. '
               'No hay auto-aprobacion: todas las cuentas pasan por revision.'),
        inline=False)
    embed.add_field(
        name='2. Chat de voz obligatorio',
        value=(f'Tus puntos solo cuentan si estuviste conectado al chat de voz del servidor (cualquier canal) al menos '
               f'{VOZ_MINIMA_MINUTOS} min acumulados. Esto evita smurfeo/boosting sin supervision.'),
        inline=False)
    embed.add_field(
        name='3. Puntaje',
        value=('El ranking se calcula por **escalado de division/liga** (subir de Hierro a Bronce, de IV a III, etc.), '
               'no solo por LP crudo. A esto se suman los Bonus otorgados por la Directiva y se restan los Castigos.'),
        inline=False)
    embed.add_field(
        name='4. Escudos Azules y maldiciones (Blue Shell)',
        value=(f'Maximo {ESCUDOS_MAX_INVENTARIO} Escudos Azules en inventario (si ganas mas con el inventario lleno, se pierden). '
               f'Con `/maldecir @jugador` gastas uno para lanzar un efecto aleatorio (hechizos fijos, campeon a elegir entre 3, '
               f'rol aleatorio, o baneo forzado). Maximo {MALDICION_MAX_ACTIVAS} maldiciones activas por victima, duran {MALDICION_DURACION_HORAS}h.'),
        inline=False)
    embed.add_field(
        name='5. Cooldown de recepcion',
        value='Top 1: sin cooldown. Top 2: 4h. Top 3-5: 6h. Resto de participantes: 12h. Se aplica sobre quien RECIBE la maldicion.',
        inline=False)
    embed.add_field(
        name='6. Reverse',
        value=('Cuanto mas abajo este tu objetivo, mas probable es que la shell rebote y el castigo lo cumplas tu. '
               'Top1: 1% - Top2: 2% - Top3: 3% - Top4: 4% - Top5: 5% - Resto: 15%. Tirar hacia arriba es mas seguro. '
               'Si sale reverse no tienes que hacer nada: rebota sola y el castigo se sortea para quien la lanzo.'),
        inline=False)
    embed.add_field(
        name='7. Restricciones de lanzamiento',
        value=('No puedes lanzar si estas en cola, en seleccion de campeon o en partida, ni en los minutos siguientes a '
               f'terminar una partida ({POSTPARTIDA_GRACIA_MINUTOS} min, mientras se procesa el resultado). '
               f'Las ultimas {TORNEO_BLOQUEO_FINAL_HORAS}h del torneo el sistema Blue Shell se desactiva por completo. '
               'Se revisa a mano y queda registrada la hora exacta de cada lanzamiento.'),
        inline=False)
    embed.add_field(
        name='8. Como conseguir una Blue Shell',
        value=('Pentakill (2), Cuadrakill, 22 kills, 30 asistencias, racha de 6 victorias, comeback de 7.000 de oro, '
               'KDA perfecto superior a 20, ganar una partida de 40+ min, cada 5 victorias con un campeon distinto, '
               'cada 5 victorias jugando con castigo, o ganarle a alguien con una Blue Shell (se la robas). '
               'Se detecta automaticamente tras cada partida. La Directiva tambien puede otorgarlas con `/otorgar_escudo`.'),
        inline=False)
    embed.add_field(
        name='9. Drop diario',
        value='Desactivado por defecto. Si se activa, la Directiva lanza un reto sencillo y se lo lleva el primero que lo cumpla (`/reclamar_reto` + confirmacion de la Directiva).',
        inline=False)
    embed.add_field(
        name='10. Aegis (proteccion)',
        value=(f'Si un jugador acumula {CASTIGOS_PENDIENTES_PARA_AEGIS} maldiciones activas SIN cumplir, se activa '
               f'automaticamente un Aegis de {AEGIS_DURACION_HORAS}h que lo protege de nuevas maldiciones.'),
        inline=False)
    embed.add_field(
        name='11. Cumplimiento de castigos',
        value=('Se cumple en la siguiente partida posible. Excepciones: si ya habias aceptado la cola cuando llego, o si es '
               'imposible cumplirlo (ej. te toca un campeon baneado), se cumple en la siguiente que puedas. Prohibido sabotear '
               'tu propio castigo para volverlo imposible. Jugar partidas ignorando un castigo pendiente es incumplir la norma. '
               'No tienes que marcar nada: la Directiva revisa y marca como cumplido con `/cumplir_castigo` en un plazo razonable.'),
        inline=False)
    embed.add_field(
        name='12. Premios',
        value=(f'Ganador general: **${PREMIO_GANADOR_USD} USD** en efectivo + insignia/rol de honor en el servidor. '
               'Tambien se otorgan insignias por logros (Cima, Podio, Ascenso, hitos de puntos, etc.).'),
        inline=False)
    embed.add_field(
        name='13. Conducta',
        value='Prohibido el uso de cuentas ajenas, boosting externo, o evadir la verificacion de voz. La Directiva puede descalificar por incumplimiento.',
        inline=False)
    embed.set_footer(text='Usa /ayuda para ver todos los comandos disponibles. Usa /terminos para ver el glosario completo.')
    await interaction.response.send_message(embed=embed)


@tree.command(name='terminos', description='Glosario de terminos del torneo (Escudos, Aegis, Escalado, etc.)')
async def terminos(interaction: discord.Interaction):
    embed = discord.Embed(
        title='Glosario de terminos - SoloQ Challenge',
        description='Definiciones oficiales de cada termino usado por el bot y el reglamento.',
        color=0xf5c518,
    )
    embed.add_field(
        name='Escudo Azul',
        value=(f'Item/moneda que un jugador acumula (maximo {ESCUDOS_MAX_INVENTARIO}) al desbloquear logros o por '
               f'hazanas otorgadas por la Directiva. Se GASTA usando `/maldecir` para lanzar una maldicion a otro jugador.'),
        inline=False)
    embed.add_field(
        name='Maldicion (Castigo)',
        value=('Efecto aleatorio que recibe un jugador cuando alguien usa `/maldecir` contra el (hechizos fijos, '
               'campeon a elegir, rol aleatorio, o baneo forzado). Es diferente de un "castigo manual" '
               '(`/castigar`), aunque ambos restan puntos o imponen una condicion.'),
        inline=False)
    embed.add_field(
        name='Pendiente / Cumplido',
        value=('Estado de una maldicion. Queda **pendiente** hasta que la Directiva confirma en partida que se '
               'ejecuto y la marca como **cumplida** con `/cumplir_castigo`. Las pendientes cuentan para activar el Aegis.'),
        inline=False)
    embed.add_field(
        name='Cooldown de recepcion',
        value='Tiempo que debe pasar antes de que alguien pueda volver a maldecir a un jugador especifico, segun su posicion: Top1 sin cooldown, Top2 4h, Top3-5 6h, resto 12h.',
        inline=False)
    embed.add_field(
        name='Reverse',
        value='Rebote de la maldicion hacia quien la lanzo. La probabilidad depende de la posicion del objetivo (1% a 15%, mas seguro tirar hacia arriba).',
        inline=False)
    embed.add_field(
        name='Aegis (proteccion)',
        value=(f'Escudo TEMPORAL distinto del Escudo Azul: se activa automaticamente por {AEGIS_DURACION_HORAS}h cuando '
               f'un jugador acumula {CASTIGOS_PENDIENTES_PARA_AEGIS} maldiciones activas sin cumplir. Mientras dura, nadie '
               f'puede maldecirlo. No se gasta ni se otorga manualmente, es automatico.'),
        inline=False)
    embed.add_field(
        name='Drop diario',
        value='Sistema opcional (desactivado por defecto) donde la Directiva lanza un reto y el primero en cumplirlo gana una Blue Shell.',
        inline=False)
    embed.add_field(
        name='Escalado',
        value=('Metrica de progreso del torneo: subir de division/liga (Hierro-Bronce-...-Retador) y de sub-rango '
               '(IV-III-II-I) dentro de cada una, en vez de solo contar LP crudo.'),
        inline=False)
    embed.add_field(
        name='High Elo / Low Elo',        value='Categorias del torneo asignadas manualmente por la Directiva con `/clasificar` tras revisar la cuenta.',
        inline=False)
    embed.set_footer(text='Usa /reglamento para ver las reglas completas.')
    await interaction.response.send_message(embed=embed)


@tree.command(name='participantes', description='Lista a todos los jugadores registrados en el torneo')
async def participantes(interaction: discord.Interaction):
    await interaction.response.defer()
    db = cargar_db()
    validos = jugadores_validos(db)
    if not validos:
        await interaction.followup.send('Aun no hay participantes registrados.')
        return
    lineas = []
    for data in sorted(validos.values(), key=lambda d: d.get('nombre', '')):
        if not data.get('elo'):
            cat = 'Sin clasificar'
        elif data.get('estado') != 'aprobado':
            cat = 'Pendiente de revision'
        else:
            cat = 'High Elo' if data['elo'] == 'high' else 'Low Elo'
        lineas.append(f'- **{data["nombre"]}** - <@{data["discord_id"]}> - {cat}')

    embed = discord.Embed(title=f'Participantes del torneo ({len(validos)})', color=0x5865F2)
    bloque = ''
    partes = []
    for linea in lineas:
        if len(bloque) + len(linea) + 1 > 1000:
            partes.append(bloque)
            bloque = ''
        bloque += linea + '\n'
    if bloque:
        partes.append(bloque)
    for i, parte in enumerate(partes[:5], 1):
        embed.add_field(name=f'Lista {i}' if len(partes) > 1 else 'Lista', value=parte, inline=False)
    await interaction.followup.send(embed=embed)


@tree.command(name='tabla', description='Clasificacion por categorias')
async def tabla(interaction: discord.Interaction):
    await interaction.response.defer()
    await mostrar_tabla(interaction.channel)
    await interaction.followup.send('Tabla actualizada.')


async def mostrar_tabla(canal):
    db = cargar_db()
    if not jugadores_validos(db):
        await canal.send('No hay jugadores registrados.')
        return
    high, low, pendientes, sin_voz = calcular_tabla(db)

    embed = discord.Embed(title='Clasificacion del Torneo SoloQ Challenge', color=0x00ff00,
                          timestamp=datetime.datetime.now())
    if high:
        embed.add_field(name='High Elo (Master, GM, Challenger)', value='​', inline=False)
        for i, j in enumerate(high, 1):
            user = canal.guild.get_member(int(j['discord_id']))
            mention = user.mention if user else j['nombre']
            embed.add_field(
                name=f'{i}. {j["nombre"]}',
                value=f'{mention} -> {j["total"]} pts (Escalado: {j["escalado"]}, Bonus: +{j["bonus"]}, Castigos: -{j["castigos"]}, Voz: {j["tiempo_voz_min"]} min)',
                inline=False
            )
    if low:
        embed.add_field(name='Low Elo (Hierro - Diamante)', value='​', inline=False)
        for i, j in enumerate(low, 1):
            user = canal.guild.get_member(int(j['discord_id']))
            mention = user.mention if user else j['nombre']
            embed.add_field(
                name=f'{i}. {j["nombre"]}',
                value=f'{mention} -> {j["total"]} pts (Escalado: {j["escalado"]}, Bonus: +{j["bonus"]}, Castigos: -{j["castigos"]}, Voz: {j["tiempo_voz_min"]} min)',
                inline=False
            )
    if sin_voz:
        embed.add_field(name='Sin verificar (falta chat de voz)', value='​', inline=False)
        for j in sin_voz:
            faltante = round(VOZ_MINIMA_MINUTOS - j['tiempo_voz_min'], 1)
            embed.add_field(name=j['nombre'], value=f'Conectado {j["tiempo_voz_min"]} min - le faltan {faltante} min en voz', inline=False)
    if pendientes:
        embed.add_field(name='Pendientes de revision / clasificacion', value='​', inline=False)
        for j in pendientes:
            embed.add_field(name=j['nombre'], value='Esperando clasificacion de la directiva (`/clasificar`)', inline=False)
    embed.set_footer(text=f'{calcular_estado_torneo(db)} - Web: ver enlace fijado')
    await canal.send(embed=embed)
    await procesar_logros_y_roles(canal, high, low, db)


@tree.command(name='ayuda', description='Muestra todos los comandos disponibles')
async def ayuda(interaction: discord.Interaction):
    embed = discord.Embed(title='SoloQ Challenge - Comandos', color=0x5865F2,
                           description=calcular_estado_torneo(cargar_db()))
    embed.add_field(
        name='Jugadores',
        value=('`/registrar` - Inscribete con tu Riot ID (Nombre#TAG). Tu cuenta queda pendiente de clasificacion\n'
               '`/progreso` - Consulta tu propio avance\n'
               '`/perfil` - Tu tarjeta completa (posicion, logros, escudos, Aegis, etc.)\n'
               '`/tabla` - Muestra la clasificacion al instante\n'
               '`/participantes` - Lista a todos los inscritos y su categoria\n'
               '`/reglamento` - Muestra el reglamento completo del torneo\n'
               '`/terminos` - Glosario: Escudo Azul, Aegis, Escalado, Pendiente/Cumplido, etc.\n'
               '`/escudos` - Ve tus Escudos Azules y maldiciones activas\n'
               '`/maldecir` - Gasta un Escudo Azul y maldice a otro jugador\n'
               '`/elegir_campeon` - Elige tu campeon si te tocó maldicion de campeon\n'
               '`/reclamar_reto` - Avisa que cumpliste el reto del drop diario (si esta activo)'),
        inline=False
    )
    embed.add_field(
        name=f'Directiva ({ROL_DIRECTIVA_NOMBRE} o Administrador)',
        value=('`/bonus` - Otorga puntos extra a un jugador\n'
               '`/castigar` - Aplica una penalizacion manual\n'
               '`/otorgar_escudo` - Da un Escudo Azul por una hazana\n'
               '`/cumplir_castigo` - Marca la maldicion mas antigua sin cumplir de un jugador como cumplida\n'
               '`/historial` - Revisa el historial de cambios de un jugador\n'
               '`/pendientes` - Lista cuentas esperando clasificacion\n'
               '`/clasificar` - Asigna categoria (High/Low Elo) a una cuenta pendiente\n'
               '`/iniciar_torneo` - Comienza oficialmente el torneo y reinicia el progreso de pruebas\n'
               '`/drop_diario` - Activa o desactiva el drop diario\n'
               '`/lanzar_reto` - Lanza el reto del drop diario\n'
               '`/confirmar_reto` - Confirma quien cumplio el reto y le da el escudo\n'
               '`/reiniciar_registro` - PELIGRO: borra todo para reiniciar con cuentas nuevas'),
        inline=False
    )
    embed.add_field(
        name='Regla de chat de voz',
        value=f'Tus puntos solo cuentan en la tabla si has estado conectado al chat de voz del servidor (cualquier canal) al menos {VOZ_MINIMA_MINUTOS} min acumulados.',
        inline=False
    )
    embed.add_field(
        name='Escudos Azules (maldiciones)',
        value=(f'Se ganan automaticamente por hazanas en partida (pentakill, 22 kills, 30 asistencias, rachas, comeback de oro, '
               f'KDA perfecto, victorias largas, etc.) o si la Directiva las otorga, maximo {ESCUDOS_MAX_INVENTARIO} en inventario. '
               f'Usa `/maldecir` para gastar uno. Maximo {MALDICION_MAX_ACTIVAS} maldiciones activas por victima. Cooldown de '
               f'recepcion segun posicion del objetivo (Top1 0h, Top2 4h, Top3-5 6h, resto 12h). Si acumulas '
               f'{CASTIGOS_PENDIENTES_PARA_AEGIS} sin cumplir se activa un Aegis (proteccion) de {AEGIS_DURACION_HORAS}h. '
               f'Usa `/terminos` o `/reglamento` para el detalle completo.'),
        inline=False
    )
    embed.add_field(
        name='Web',
        value='La clasificacion tambien esta disponible en la pagina web del torneo (se actualiza sola).',
        inline=False
    )
    embed.set_footer(text='Categorias: Low Elo (Hierro-Diamante) - High Elo (Master-Retador)')
    await interaction.response.send_message(embed=embed)


@tree.command(name='pendientes', description='(Directiva) Lista cuentas pendientes de clasificacion')
async def pendientes(interaction: discord.Interaction):
    if not await requiere_directiva(interaction):
        return
    await interaction.response.defer()
    db = cargar_db()
    _, _, pend, _ = calcular_tabla(db)
    if not pend:
        await interaction.followup.send('No hay cuentas pendientes de clasificacion.')
        return
    mensaje = '**Cuentas pendientes de clasificacion:**\n'
    for j in pend:
        extra = f' - elo previo declarado: **{j["elo_previo"]}**' if j.get('elo_previo') else ''
        mensaje += f'- **{j["nombre"]}** - {j["tier_actual"]} {j["rank_actual"]} - <@{j["discord_id"]}>{extra}\n'
    mensaje += '\nUsa `/clasificar usuario:@jugador categoria:low|high` para clasificar.'
    await interaction.followup.send(mensaje)


@tree.command(name='clasificar', description='(Directiva) Asigna categoria High/Low Elo a una cuenta pendiente')
@app_commands.describe(usuario='Jugador a clasificar', categoria='low o high')
@app_commands.choices(categoria=[
    app_commands.Choice(name='Low Elo', value='low'),
    app_commands.Choice(name='High Elo', value='high'),
])
async def clasificar(interaction: discord.Interaction, usuario: discord.Member, categoria: app_commands.Choice[str]):
    if not await requiere_directiva(interaction):
        return
    await interaction.response.defer()
    db = cargar_db()
    for puuid, data in jugadores_validos(db).items():
        if data['discord_id'] == str(usuario.id):
            data['estado'] = 'aprobado'
            data['elo'] = categoria.value
            guardar_db(db)
            await interaction.followup.send(
                f'<@{usuario.id}> fuiste clasificado en **{"High" if categoria.value == "high" else "Low"} Elo**. '
                f'Ya apareces en la tabla (si cumples el requisito de voz).')
            return
    await interaction.followup.send('Usuario no encontrado en el torneo.')


@tree.command(name='escudos', description='Consulta tus Escudos Azules y maldiciones activas')
async def escudos(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = str(interaction.user.id)
    db = cargar_db()
    for puuid, data in jugadores_validos(db).items():
        if data['discord_id'] == user_id:
            activas = maldiciones_activas_de(data)
            malds_txt = '\n'.join(
                f'- {m["texto"]} (de <@{m["de"]}>)' + (' [cumplido]' if m.get('cumplido') else ' [pendiente]')
                for m in activas
            ) or 'Ninguna'
            pos = posicion_de_jugador(db, puuid)
            cd_horas = cooldown_recepcion_horas(pos)
            cd_txt = 'sin cooldown (eres Top 1)' if cd_horas == 0 else f'{cd_horas}h'
            restante_recepcion = 0
            if data.get('ultima_maldicion_recibida'):
                try:
                    transcurridas = (datetime.datetime.now() - datetime.datetime.fromisoformat(data['ultima_maldicion_recibida'])).total_seconds() / 3600
                    restante_recepcion = max(cd_horas - transcurridas, 0)
                except Exception:
                    pass
            proteccion_txt = f'Protegido {round(restante_recepcion, 1)}h mas (cooldown de recepcion)' if restante_recepcion > 0 else f'Sin proteccion activa (cooldown de recepcion segun tu posicion: {cd_txt})'
            aegis_txt = f'Activo - {round(aegis_restante_horas(data), 1)}h restantes (nadie puede maldecirte)' if aegis_activo(data) else 'Inactivo'
            await interaction.followup.send(
                f'**{data["nombre"]}**\n'
                f'Escudos Azules disponibles: **{data.get("escudos", 0)}/{ESCUDOS_MAX_INVENTARIO}**\n'
                f'{proteccion_txt}\n'
                f'Aegis (proteccion): {aegis_txt}\n'
                f'Maldiciones activas sobre ti ({len(activas)}/{MALDICION_MAX_ACTIVAS}):\n{malds_txt}'
            )
            return
    await interaction.followup.send('No estas registrado.')


@tree.command(name='maldecir', description='Gasta un Escudo Azul para lanzar una maldicion aleatoria a otro jugador')
@app_commands.describe(usuario='Jugador objetivo')
async def maldecir(interaction: discord.Interaction, usuario: discord.Member):
    await interaction.response.defer()
    caster_id = str(interaction.user.id)
    target_id = str(usuario.id)
    if caster_id == target_id:
        await interaction.followup.send('No puedes maldecirte a ti mismo.')
        return
    db = cargar_db()
    valid = jugadores_validos(db)
    caster_puuid = caster_data = None
    target_puuid = target_data = None
    for puuid, data in valid.items():
        if data['discord_id'] == caster_id:
            caster_puuid, caster_data = puuid, data
        if data['discord_id'] == target_id:
            target_puuid, target_data = puuid, data

    if caster_data is None:
        await interaction.followup.send('No estas registrado en el torneo.')
        return
    if caster_data.get('estado') != 'aprobado':
        await interaction.followup.send('Tu cuenta debe estar aprobada por la directiva para poder maldecir.')
        return
    if target_data is None or target_data.get('estado') != 'aprobado':
        await interaction.followup.send('Ese jugador no esta registrado/aprobado en el torneo.')
        return
    if aegis_activo(target_data):
        await interaction.followup.send(
            f'**{target_data["nombre"]}** tiene un Aegis activo ({round(aegis_restante_horas(target_data), 1)}h restantes) y no puede ser maldecido.')
        return
    if caster_data.get('escudos', 0) <= 0:
        await interaction.followup.send('No tienes Escudos Azules disponibles. Ganalos desbloqueando logros o pidiendole uno a la directiva por una hazana.')
        return

    if torneo_en_ultimas_48h(db):
        await interaction.followup.send(
            f'El sistema Blue Shell esta desactivado: quedan menos de {TORNEO_BLOQUEO_FINAL_HORAS}h para el cierre del torneo.')
        return

    # Cooldown de recepcion: depende de la posicion del OBJETIVO en la tabla.
    pos_objetivo = posicion_de_jugador(db, target_puuid)
    cd_horas = cooldown_recepcion_horas(pos_objetivo)
    ultima_recibida = target_data.get('ultima_maldicion_recibida')
    if ultima_recibida:
        try:
            transcurridas = (datetime.datetime.now() - datetime.datetime.fromisoformat(ultima_recibida)).total_seconds() / 3600
            restante_recepcion = cd_horas - transcurridas
            if restante_recepcion > 0:
                await interaction.followup.send(
                    f'**{target_data["nombre"]}** esta en cooldown de recepcion (le llego una maldicion hace poco). '
                    f'Podras intentarlo de nuevo en {round(restante_recepcion, 1)} horas.')
                return
        except Exception:
            pass

    # Restricciones de lanzamiento: no en cola/partida/postpartida (segun API publica: partida en vivo o postpartida).
    motivo_bloqueo = motivo_bloqueo_por_partida(caster_puuid, caster_data)
    if motivo_bloqueo:
        await interaction.followup.send(f'No puedes lanzar una maldicion ahora mismo: {motivo_bloqueo}.')
        return

    efecto = generar_efecto_maldicion(pos_objetivo)
    destino_puuid, destino_data = target_puuid, target_data
    if efecto['tipo'] == 'reverse':
        destino_puuid, destino_data = caster_puuid, caster_data

    if aegis_activo(destino_data):
        await interaction.followup.send(
            f'La maldicion iba a rebotar hacia **{destino_data["nombre"]}**, pero tiene un Aegis activo y la maldicion se disipa. Se consumio tu escudo igualmente.')
        caster_data['escudos'] = caster_data.get('escudos', 0) - 1
        caster_data['ultimo_escudo_uso'] = str(datetime.datetime.now())
        guardar_db(db)
        return

    if len(maldiciones_activas_de(destino_data)) >= MALDICION_MAX_ACTIVAS:
        await interaction.followup.send(
            f'**{destino_data["nombre"]}** ya tiene el maximo de {MALDICION_MAX_ACTIVAS} maldiciones activas ahora mismo. '
            f'Intenta con otro objetivo o espera a que expiren (dura {MALDICION_DURACION_HORAS}h).')
        return

    ahora = str(datetime.datetime.now())
    caster_data['escudos'] = caster_data.get('escudos', 0) - 1
    caster_data['ultimo_escudo_uso'] = ahora
    destino_data['ultima_maldicion_recibida'] = ahora
    destino_data.setdefault('maldiciones', []).append({
        'tipo': efecto['tipo'], 'texto': efecto['texto'], 'opciones': efecto['opciones'],
        'elegido': efecto['elegido'], 'de': caster_id, 'fecha': ahora, 'cumplido': False,
    })

    # Aegis: si el destino acumula demasiados castigos activos sin cumplir, se protege temporalmente.
    pendientes_destino = castigos_pendientes_de(destino_data)
    aegis_otorgado = False
    if len(pendientes_destino) >= CASTIGOS_PENDIENTES_PARA_AEGIS:
        destino_data['escudo_hasta'] = str(datetime.datetime.now() + datetime.timedelta(hours=AEGIS_DURACION_HORAS))
        aegis_otorgado = True

    guardar_db(db)

    registros = cargar_registros()
    registros.append({
        'tipo': 'maldecir', 'usuario': caster_id, 'nombre': caster_data['nombre'],
        'puntos': 0, 'motivo': f"objetivo original:{target_data['nombre']} | efecto:{efecto['tipo']} | destino final:{destino_data['nombre']}",
        'fecha': ahora,
    })
    guardar_registros(registros)

    embed = discord.Embed(title='Maldicion lanzada!', color=0x9b59b6, timestamp=datetime.datetime.now())
    embed.add_field(name='Lanzada por', value=f'<@{caster_id}>', inline=True)
    embed.add_field(name='Objetivo original', value=f'**{target_data["nombre"]}**', inline=True)
    embed.add_field(name='Objetivo final', value=f'**{destino_data["nombre"]}**' + (' (REVERSE)' if efecto['tipo'] == 'reverse' else ''), inline=True)
    if efecto['tipo'] == 'campeon':
        opciones_txt = ', '.join(efecto['opciones'])
        embed.add_field(name='Efecto', value=f'{efecto["texto"]}\nOpciones: **{opciones_txt}**', inline=False)
        embed.set_thumbnail(url=icono_campeon(efecto['opciones'][0]))
    else:
        embed.add_field(name='Efecto', value=efecto['texto'], inline=False)
    cd_destino_txt = 'sin cooldown (Top 1)' if cooldown_recepcion_horas(pos_objetivo) == 0 else f'{cooldown_recepcion_horas(pos_objetivo)}h de cooldown de recepcion'
    embed.set_footer(text=f'Dura {MALDICION_DURACION_HORAS}h - Maximo {MALDICION_MAX_ACTIVAS} activas por jugador - El objetivo original tenia {cd_destino_txt}')
    await interaction.followup.send(
        content=f'<@{destino_data["discord_id"]}> te lanzaron una maldicion Blue Shell!',
        embed=embed)
    if aegis_otorgado:
        await interaction.followup.send(
            f'<@{destino_data["discord_id"]}> acumulaste {CASTIGOS_PENDIENTES_PARA_AEGIS} castigos (maldiciones) sin cumplir: '
            f'se activo tu **Aegis** (proteccion) por {AEGIS_DURACION_HORAS}h. Nadie podra maldecirte mientras dure.')


@tree.command(name='elegir_campeon', description='Elige tu campeon si te toco una maldicion de campeon aleatorio')
@app_commands.describe(campeon='El campeon que eliges entre las opciones dadas')
async def elegir_campeon(interaction: discord.Interaction, campeon: str):
    await interaction.response.defer()
    user_id = str(interaction.user.id)
    db = cargar_db()
    for puuid, data in jugadores_validos(db).items():
        if data['discord_id'] != user_id:
            continue
        pendiente = None
        for m in maldiciones_activas_de(data):
            if m.get('tipo') == 'campeon' and not m.get('elegido'):
                pendiente = m
                break
        if pendiente is None:
            await interaction.followup.send('No tienes ninguna maldicion de campeon pendiente por elegir.')
            return
        opciones_norm = {o.lower(): o for o in pendiente['opciones']}
        if campeon.lower() not in opciones_norm:
            await interaction.followup.send(f'Debes elegir uno de estos: **{", ".join(pendiente["opciones"])}**.')
            return
        elegido = opciones_norm[campeon.lower()]
        pendiente['elegido'] = elegido
        guardar_db(db)
        embed = discord.Embed(title='Campeon elegido', description=f'**{data["nombre"]}** jugara **{elegido}** en su proxima partida.', color=0x9b59b6)
        embed.set_thumbnail(url=icono_campeon(elegido))
        await interaction.followup.send(embed=embed)
        return
    await interaction.followup.send('No estas registrado.')


@tree.command(name='cumplir_castigo', description='(Directiva) Marca la maldicion mas antigua sin cumplir de un jugador como cumplida')
@app_commands.describe(usuario='Jugador que cumplio su castigo')
async def cumplir_castigo(interaction: discord.Interaction, usuario: discord.Member):
    if not await requiere_directiva(interaction):
        return
    await interaction.response.defer()
    db = cargar_db()
    for puuid, data in jugadores_validos(db).items():
        if data['discord_id'] == str(usuario.id):
            pendientes_j = castigos_pendientes_de(data)
            if not pendientes_j:
                await interaction.followup.send(f'**{data["nombre"]}** no tiene castigos pendientes por cumplir.')
                return
            # marca la mas antigua
            pendientes_j.sort(key=lambda m: m.get('fecha', ''))
            objetivo = pendientes_j[0]
            for m in data.get('maldiciones', []):
                if m is objetivo or (m.get('fecha') == objetivo.get('fecha') and m.get('texto') == objetivo.get('texto')):
                    m['cumplido'] = True
                    break
            guardar_db(db)
            await interaction.followup.send(
                f'<@{usuario.id}> tu castigo fue marcado como **cumplido** por la Directiva: "{objetivo["texto"]}"')
            return
    await interaction.followup.send('Usuario no encontrado en el torneo.')


@tree.command(name='otorgar_escudo', description='(Directiva) Otorga un Escudo Azul por una hazana dentro del juego')
@app_commands.describe(usuario='Jugador a premiar', motivo='ej. Primera Sangre, Penta Kill, Ace')
async def otorgar_escudo(interaction: discord.Interaction, usuario: discord.Member, motivo: str = ""):
    if not await requiere_directiva(interaction):
        return
    await interaction.response.defer()
    db = cargar_db()
    for puuid, data in jugadores_validos(db).items():
        if data['discord_id'] == str(usuario.id):
            if data.get('escudos', 0) >= ESCUDOS_MAX_INVENTARIO:
                await interaction.followup.send(f'**{data["nombre"]}** ya tiene el maximo de {ESCUDOS_MAX_INVENTARIO} Escudos Azules en inventario.')
                return
            data['escudos'] = data.get('escudos', 0) + 1
            guardar_db(db)
            await interaction.followup.send(
                f'<@{usuario.id}> recibiste un **Escudo Azul** (item para lanzar `/maldecir`). Motivo: {motivo or "N/A"}. '
                f'Total: {data["escudos"]}/{ESCUDOS_MAX_INVENTARIO}.')
            return
    await interaction.followup.send('Usuario no encontrado en el torneo.')


@tree.command(name='reiniciar_registro', description='(Directiva) PELIGRO: borra TODOS los registros para empezar con cuentas nuevas')
@app_commands.describe(confirmar='Escribe SI (mayusculas) para confirmar el borrado total')
async def reiniciar_registro(interaction: discord.Interaction, confirmar: str):
    if not await requiere_directiva(interaction):
        return
    await interaction.response.defer()
    if confirmar != 'SI':
        await interaction.followup.send(
            'Accion cancelada. Escribe `confirmar: SI` (en mayusculas) para confirmar el borrado total de TODOS los jugadores registrados '
            '(incluye LP, bonus, castigos, logros, escudos y tiempo de voz acumulado). Usa esto solo cuando pasen a las cuentas nuevas.')
        return
    guardar_db({})
    await interaction.followup.send(
        'Se borraron todos los registros. Todos deben usar `/registrar` de nuevo con sus cuentas nuevas '
        '(pueden usar `elo_previo` para que la directiva sepa su nivel real al revisar).')


@tree.command(name='iniciar_torneo', description='(Directiva) Inicia oficialmente el torneo y reinicia el progreso de pruebas')
async def iniciar_torneo(interaction: discord.Interaction):
    if not await requiere_directiva(interaction):
        return
    await interaction.response.defer()
    db = cargar_db()
    ahora = datetime.datetime.now()
    contador = 0
    for puuid, data in jugadores_validos(db).items():
        if data.get('estado') != 'aprobado':
            continue
        info = obtener_info_ranked(data['nombre'], data['region'])
        if info is None:
            continue
        data['lp_inicial'] = info['lp']
        data['tier_inicial'] = info['tier']
        data['rank_inicial'] = info['rank']
        data['bonus_total'] = 0
        data['castigos_total'] = 0
        data['logros'] = []
        contador += 1
    db['inicio_torneo'] = str(ahora)
    db['torneo_iniciado'] = True
    guardar_db(db)
    await interaction.followup.send(
        f'Torneo iniciado oficialmente. Se reinicio el progreso de {contador} jugadores aprobados '
        f'(el conteo de escalado arranca desde ahora, el tiempo de voz acumulado se conserva). Dura {DURACION_TORNEO} dias.')
    if CANAL_CLASIFICACION_ID != 0:
        canal = client.get_channel(CANAL_CLASIFICACION_ID)
        if canal:
            await canal.send('**EL TORNEO HA COMENZADO OFICIALMENTE!** Buena suerte a todos.')
            await mostrar_tabla(canal)


@tree.command(name='castigar', description='(Directiva) Resta puntos a un jugador')
@app_commands.describe(usuario='Jugador a castigar', puntos='Puntos a restar', motivo='Razon del castigo')
async def castigar(interaction: discord.Interaction, usuario: discord.Member, puntos: int, motivo: str = ""):
    if not await requiere_directiva(interaction):
        return
    await interaction.response.defer()
    if puntos <= 0:
        await interaction.followup.send('Los puntos deben ser positivos.')
        return
    db = cargar_db()
    for puuid, data in jugadores_validos(db).items():
        if data['discord_id'] == str(usuario.id):
            data['castigos_total'] = data.get('castigos_total', 0) + puntos
            guardar_db(db)
            registros = cargar_registros()
            registros.append({
                'tipo': 'castigo', 'usuario': str(usuario.id), 'nombre': data['nombre'],
                'puntos': puntos, 'motivo': motivo, 'fecha': str(datetime.datetime.now()),
                'admin': str(interaction.user.id)
            })
            guardar_registros(registros)
            await interaction.followup.send(
                f'<@{usuario.id}> recibiste un castigo de **-{puntos} puntos**.\n'
                f'Motivo: {motivo}\nTotal castigos: -{data["castigos_total"]}')
            return
    await interaction.followup.send('Usuario no encontrado en el torneo.')


@tree.command(name='bonus', description='(Directiva) Otorga puntos extra a un jugador')
@app_commands.describe(usuario='Jugador a bonificar', puntos='Puntos a sumar', motivo='Razon (ej. Penta, Primera Sangre)')
async def bonus(interaction: discord.Interaction, usuario: discord.Member, puntos: int, motivo: str = ""):
    if not await requiere_directiva(interaction):
        return
    await interaction.response.defer()
    if puntos <= 0:
        await interaction.followup.send('Los puntos deben ser positivos.')
        return
    db = cargar_db()
    for puuid, data in jugadores_validos(db).items():
        if data['discord_id'] == str(usuario.id):
            data['bonus_total'] = data.get('bonus_total', 0) + puntos
            guardar_db(db)
            registros = cargar_registros()
            registros.append({
                'tipo': 'bonus', 'usuario': str(usuario.id), 'nombre': data['nombre'],
                'puntos': puntos, 'motivo': motivo, 'fecha': str(datetime.datetime.now()),
                'admin': str(interaction.user.id)
            })
            guardar_registros(registros)
            await interaction.followup.send(
                f'<@{usuario.id}> recibiste un bonus de **+{puntos} puntos**.\n'
                f'Motivo: {motivo}\nTotal bonus: +{data["bonus_total"]}')
            return
    await interaction.followup.send('Usuario no encontrado en el torneo.')


@tree.command(name='historial', description='(Directiva) Ver historial de bonus y castigos')
@app_commands.describe(usuario='Jugador')
async def historial(interaction: discord.Interaction, usuario: discord.Member):
    if not await requiere_directiva(interaction):
        return
    await interaction.response.defer()
    registros = cargar_registros()
    filtrados = [r for r in registros if r['usuario'] == str(usuario.id)]
    if not filtrados:
        await interaction.followup.send('No hay registros para este jugador.')
        return
    mensaje = f'Historial de {usuario.display_name}:\n'
    for r in filtrados[-10:]:
        simbolo = 'CASTIGO' if r['tipo'] == 'castigo' else 'BONUS'
        mensaje += f'[{simbolo}] {r["puntos"]} pts - {r["motivo"]} ({r["fecha"][:10]})\n'
    await interaction.followup.send(mensaje)


@tree.command(name='drop_diario', description='(Directiva) Activa o desactiva el sistema de drop diario de Escudos Azules')
@app_commands.describe(activo='True para activar, False para desactivar')
async def drop_diario(interaction: discord.Interaction, activo: bool):
    if not await requiere_directiva(interaction):
        return
    await interaction.response.defer()
    db = cargar_db()
    db['drop_diario_activo'] = activo
    guardar_db(db)
    await interaction.followup.send(f'Drop diario {"activado" if activo else "desactivado"}.')


@tree.command(name='lanzar_reto', description='(Directiva) Lanza el reto del drop diario (ej. primer triple kill, primer First Blood)')
@app_commands.describe(texto='Descripcion del reto a cumplir')
async def lanzar_reto(interaction: discord.Interaction, texto: str):
    if not await requiere_directiva(interaction):
        return
    await interaction.response.defer()
    db = cargar_db()
    if not db.get('drop_diario_activo'):
        await interaction.followup.send('El drop diario esta desactivado. Actívalo primero con `/drop_diario activo:True`.')
        return
    db['reto_activo_texto'] = texto
    db['reto_activo_fecha'] = str(datetime.datetime.now())
    guardar_db(db)
    await interaction.followup.send(
        f'**Reto del dia lanzado:** {texto}\n'
        f'Se lo lleva el primero que lo cumpla en una partida iniciada a partir de ahora. '
        f'Usa `/reclamar_reto` cuando lo logres (la Directiva debe confirmarlo con `/confirmar_reto`).')


@tree.command(name='reclamar_reto', description='Avisa a la Directiva que cumpliste el reto del drop diario')
async def reclamar_reto(interaction: discord.Interaction):
    await interaction.response.defer()
    db = cargar_db()
    if not db.get('drop_diario_activo') or not db.get('reto_activo_texto'):
        await interaction.followup.send('No hay ningun reto activo ahora mismo.')
        return
    await interaction.followup.send(
        f'{interaction.user.mention} reclama haber cumplido el reto: "{db["reto_activo_texto"]}". '
        f'La Directiva debe confirmarlo con `/confirmar_reto usuario:{interaction.user.mention}`.')


@tree.command(name='confirmar_reto', description='(Directiva) Confirma que un jugador cumplio el reto del drop diario y le da el escudo')
@app_commands.describe(usuario='Jugador que cumplio el reto')
async def confirmar_reto(interaction: discord.Interaction, usuario: discord.Member):
    if not await requiere_directiva(interaction):
        return
    await interaction.response.defer()
    db = cargar_db()
    if not db.get('reto_activo_texto'):
        await interaction.followup.send('No hay ningun reto activo para confirmar.')
        return
    for puuid, data in jugadores_validos(db).items():
        if data['discord_id'] == str(usuario.id):
            if data.get('escudos', 0) >= ESCUDOS_MAX_INVENTARIO:
                await interaction.followup.send(f'**{data["nombre"]}** ya tiene el inventario de escudos lleno, no se otorgo.')
            else:
                data['escudos'] = data.get('escudos', 0) + 1
            texto_reto = db['reto_activo_texto']
            db['reto_activo_texto'] = ''
            db['reto_activo_fecha'] = ''
            guardar_db(db)
            await interaction.followup.send(
                f'<@{usuario.id}> gano el drop diario: "{texto_reto}". Recibio un **Escudo Azul**. '
                f'Total: {data.get("escudos", 0)}/{ESCUDOS_MAX_INVENTARIO}.')
            return
    await interaction.followup.send('Usuario no encontrado en el torneo.')


# ------------------- OBTENCION AUTOMATICA DE ESCUDOS (Match-V5) -------------------

def _kda_valor(kills, deaths, assists):
    if deaths == 0:
        return float(kills + assists)
    return (kills + assists) / deaths


async def _procesar_partida_jugador(puuid, data, validos, headers):
    """Revisa la ultima partida ranked del jugador y otorga Escudos Azules automaticos segun corresponda.
    Devuelve una lista de textos de anuncio (puede estar vacia) y modifica 'data' in-place."""
    anuncios = []
    region_base = REGION_MAP.get(data.get('region', 'lan').lower())
    if not region_base:
        return anuncios
    try:
        url = f'https://{region_base}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?count=1'
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code != 200 or not r.json():
            return anuncios
        match_id = r.json()[0]
        if match_id == data.get('ultimo_match_procesado'):
            return anuncios

        url2 = f'https://{region_base}.api.riotgames.com/lol/match/v5/matches/{match_id}'
        r2 = requests.get(url2, headers=headers, timeout=8)
        if r2.status_code != 200:
            return anuncios
        match = r2.json()
        info_match = match['info']
        data['ultimo_match_procesado'] = match_id

        if info_match.get('gameDuration', 0) < 300 or info_match.get('queueId') != 420:
            return anuncios  # remake/partida invalida o no es Ranked Solo/Duo, se marca como visto pero no se evalua

        participante = next((p for p in info_match['participants'] if p['puuid'] == puuid), None)
        if participante is None:
            return anuncios

        gano = participante.get('win', False)
        kills = participante.get('kills', 0)
        deaths = participante.get('deaths', 0)
        assists = participante.get('assists', 0)
        penta = participante.get('pentaKills', 0)
        quadra = participante.get('quadraKills', 0)
        duracion_min = info_match.get('gameDuration', 0) / 60
        champ = participante.get('championName', 'Desconocido')

        def ganar_escudos(cantidad, motivo):
            espacio = max(ESCUDOS_MAX_INVENTARIO - data.get('escudos', 0), 0)
            otorgar = min(cantidad, espacio)
            if otorgar > 0:
                data['escudos'] = data.get('escudos', 0) + otorgar
                anuncios.append(f'{motivo} (+{otorgar} Escudo{"s" if otorgar != 1 else ""} Azul{"es" if otorgar != 1 else ""})')
            elif cantidad > 0:
                anuncios.append(f'{motivo} (inventario de escudos lleno, no se otorgo)')

        if penta > 0:
            ganar_escudos(2, 'Pentakill')
        if quadra > 0:
            ganar_escudos(1, 'Cuadrakill')
        if kills >= 22:
            ganar_escudos(1, f'{kills} kills en una partida')
        if assists >= 30:
            ganar_escudos(1, f'{assists} asistencias en una partida')
        kda = _kda_valor(kills, deaths, assists)
        if kda > 20:
            ganar_escudos(1, f'KDA perfecto ({round(kda, 1)})')
        if gano and duracion_min >= 40:
            ganar_escudos(1, f'Victoria de {round(duracion_min)} min')

        if gano:
            data['racha_victorias'] = data.get('racha_victorias', 0) + 1
            if data['racha_victorias'] % 6 == 0:
                ganar_escudos(1, f'Racha de {data["racha_victorias"]} victorias')
        else:
            data['racha_victorias'] = 0

        if gano:
            campeones = data.get('campeones_ganados') or {}
            if not isinstance(campeones, dict):
                campeones = {}
            campeones[champ] = campeones.get(champ, 0) + 1
            data['campeones_ganados'] = campeones
            if campeones[champ] % 5 == 0:
                ganar_escudos(1, f'{campeones[champ]} victorias con {champ}')

        tenia_castigo = len(castigos_pendientes_de(data)) > 0
        if gano and tenia_castigo:
            data['victorias_con_castigo_contador'] = data.get('victorias_con_castigo_contador', 0) + 1
            if data['victorias_con_castigo_contador'] % 5 == 0:
                ganar_escudos(1, f'{data["victorias_con_castigo_contador"]} victorias jugando con castigo pendiente')

        if gano:
            try:
                pid_to_team = {p['participantId']: p['teamId'] for p in info_match['participants']}
                mi_team = participante['teamId']
                url3 = f'https://{region_base}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline'
                r3 = requests.get(url3, headers=headers, timeout=8)
                if r3.status_code == 200:
                    frames = r3.json()['info']['frames']
                    idx = min(15, len(frames) - 1)
                    if idx >= 0:
                        pframes = frames[idx]['participantFrames']
                        oro_propio = sum(pf['totalGold'] for pid_str, pf in pframes.items() if pid_to_team.get(int(pid_str)) == mi_team)
                        oro_rival = sum(pf['totalGold'] for pid_str, pf in pframes.items() if pid_to_team.get(int(pid_str)) != mi_team)
                        if oro_rival - oro_propio >= 7000:
                            ganar_escudos(1, 'Comeback de 7000+ de oro')
            except Exception:
                pass

        if gano:
            mi_team = participante.get('teamId')
            for p2 in info_match['participants']:
                if p2.get('teamId') == mi_team:
                    continue
                rival_data = validos.get(p2.get('puuid'))
                if rival_data and rival_data.get('escudos', 0) > 0 and data.get('escudos', 0) < ESCUDOS_MAX_INVENTARIO:
                    rival_data['escudos'] = rival_data.get('escudos', 0) - 1
                    data['escudos'] = data.get('escudos', 0) + 1
                    anuncios.append(f'Le robaste un Escudo Azul a **{rival_data["nombre"]}** por vencerlo')
    except Exception as e:
        print(f'Error procesando partida de {puuid}: {e}')
    return anuncios


@tasks.loop(minutes=DROP_DIARIO_INTERVALO_MIN)
async def revisar_partidas_recientes():
    db = cargar_db()
    validos = jugadores_validos(db)
    if not validos:
        return
    headers = {'X-Riot-Token': RIOT_API_KEY}
    anuncios_totales = []
    for puuid, data in list(validos.items()):
        if data.get('estado') != 'aprobado':
            continue
        anuncios = await _procesar_partida_jugador(puuid, data, validos, headers)
        if anuncios:
            anuncios_totales.append(f"<@{data['discord_id']}> **{data.get('nombre', '?')}**: " + '; '.join(anuncios))

    guardar_db(db)

    if anuncios_totales and CANAL_CLASIFICACION_ID != 0:
        canal = client.get_channel(CANAL_CLASIFICACION_ID)
        if canal:
            try:
                texto = '**Escudos Azules ganados en partida (automatico)**\n' + '\n'.join(anuncios_totales[:10])
                await canal.send(texto)
            except Exception:
                pass


# ------------------- TAREAS AUTOMATICAS -------------------

@tasks.loop(minutes=30)
async def actualizar_canal():
    if CANAL_CLASIFICACION_ID == 0:
        return
    canal = client.get_channel(CANAL_CLASIFICACION_ID)
    if canal is None:
        return
    await canal.purge(limit=5)
    await mostrar_tabla(canal)


@tasks.loop(minutes=5)
async def voice_checkpoint():
    for discord_id in list(VOICE_SESIONES.keys()):
        flush_voice_time(discord_id)


@client.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    discord_id = str(member.id)
    estaba_conectado = before.channel is not None
    esta_conectado = after.channel is not None
    if not estaba_conectado and esta_conectado:
        VOICE_SESIONES[discord_id] = datetime.datetime.now()
    elif estaba_conectado and not esta_conectado:
        flush_voice_time(discord_id)
        VOICE_SESIONES.pop(discord_id, None)


_comandos_sincronizados = False


@client.event
async def on_ready():
    global _comandos_sincronizados
    print(f'Bot conectado como {client.user}')
    if not _comandos_sincronizados:
        await tree.sync()
        _comandos_sincronizados = True
    ahora = datetime.datetime.now()
    for guild in client.guilds:
        for vc in guild.voice_channels:
            for m in vc.members:
                if not m.bot:
                    VOICE_SESIONES[str(m.id)] = ahora
    if not actualizar_canal.is_running():
        actualizar_canal.start()
    if not voice_checkpoint.is_running():
        voice_checkpoint.start()
    if not revisar_partidas_recientes.is_running():
        revisar_partidas_recientes.start()


# ------------------- SITIO WEB (gratis, mismo servicio) -------------------

app = Flask(__name__)

PAGINA_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="60">
<title>SoloQ Challenge</title>
<style>
  * { box-sizing: border-box; }
  body { background:#0b0d12; color:#e8e8e8; font-family: 'Segoe UI', Arial, sans-serif; margin:0; padding:0 0 60px; }
  header { background:radial-gradient(circle at top, #1a2233, #0b0d12); padding:50px 20px 30px; text-align:center; border-bottom:3px solid #f5c518; }
  header h1 { margin:0; font-size:2.8em; color:#f5c518; letter-spacing:1px; text-shadow:0 0 20px rgba(245,197,24,0.4); }
  header p { color:#9ca3af; margin-top:8px; }
  .estado { display:inline-block; margin-top:16px; padding:8px 18px; background:#1f2937; border:1px solid #f5c518; border-radius:20px; color:#f5c518; font-weight:bold; font-size:0.9em; }
  .premio { display:inline-block; margin-top:10px; margin-left:10px; padding:8px 18px; background:#132a1c; border:1px solid #3ba55d; border-radius:20px; color:#3ba55d; font-weight:bold; font-size:0.9em; }
  .stats { display:flex; justify-content:center; gap:16px; margin-top:24px; flex-wrap:wrap; }
  .stat-card { background:#161b22; border-radius:10px; padding:14px 22px; min-width:120px; text-align:center; border:1px solid #2a2f3a; }
  .stat-card .num { font-size:1.6em; color:#f5c518; font-weight:bold; }
  .stat-card .label { font-size:0.75em; color:#9ca3af; text-transform:uppercase; letter-spacing:1px; }
  .contenedor { max-width:1100px; margin:30px auto; padding:0 20px; }
  .categoria { margin-bottom:40px; }
  .categoria h2 { border-left:5px solid #f5c518; padding-left:12px; font-size:1.4em; }
  table { width:100%; border-collapse:collapse; background:#161b22; border-radius:8px; overflow:hidden; }
  th, td { padding:12px 14px; text-align:left; border-bottom:1px solid #2a2f3a; }
  th { background:#1f2530; color:#f5c518; text-transform:uppercase; font-size:0.8em; letter-spacing:1px; }
  tr:hover { background:#1c2230; }
  .pos1 { color:#f5c518; font-weight:bold; }
  .pos2 { color:#c0c0c0; font-weight:bold; }
  .pos3 { color:#cd7f32; font-weight:bold; }
  .voz-ok { color:#3ba55d; }
  .voz-no { color:#ed4245; }
  .vacio { color:#6b7280; padding:20px; text-align:center; }
  .aviso { background:#1f2937; border-left:4px solid #f5c518; padding:14px 18px; border-radius:6px; margin-bottom:20px; color:#d1d5db; }
  .badge-aegis { display:inline-block; padding:2px 8px; border-radius:10px; font-size:0.75em; font-weight:bold; background:#132a1c; color:#3ba55d; border:1px solid #3ba55d; }
  .badge-shell { display:inline-block; padding:2px 8px; border-radius:10px; font-size:0.75em; font-weight:bold; background:#241a33; color:#9b59b6; border:1px solid #9b59b6; margin-left:4px; }
  .champ-icon { width:28px; height:28px; border-radius:50%; vertical-align:middle; margin-right:6px; border:1px solid #f5c518; }
  footer { text-align:center; color:#6b7280; margin-top:40px; font-size:0.85em; }
</style>
</head>
<body>
<header>
  <h1>SoloQ Challenge</h1>
  <p>Torneo de escalado de division/liga - {{ duracion }} dias</p>
  <div class="estado">{{ estado_torneo }}</div>
  <div class="premio">Premio: ${{ premio }} USD + insignias</div>
  <div class="stats">
    <div class="stat-card"><div class="num">{{ high|length + low|length }}</div><div class="label">Jugadores activos</div></div>
    <div class="stat-card"><div class="num">{{ pendientes|length }}</div><div class="label">Pendientes de clasificacion</div></div>
    <div class="stat-card"><div class="num">{{ sin_voz|length }}</div><div class="label">Sin verificar voz</div></div>
  </div>
</header>
<div class="contenedor">
  <div class="aviso">Para que tus puntos sean validos debes conectarte al chat de voz del servidor de Discord (cualquier canal) mientras juegas tus partidas. El ranking se basa en escalar de division/liga (Blue Shell / Escudos Azules / Aegis activos).</div>
  <div class="categoria">
    <h2>High Elo</h2>
    {% if high %}
    <table>
      <tr><th>#</th><th>Jugador</th><th>Rango actual</th><th>Escalado</th><th>Bonus</th><th>Castigos</th><th>Voz</th><th>Escudos / Aegis</th><th>Total</th></tr>
      {% for j in high %}
      <tr>
        <td class="{{ 'pos1' if loop.index==1 else ('pos2' if loop.index==2 else ('pos3' if loop.index==3 else '')) }}">{{ loop.index }}</td>
        <td>{{ j.nombre }}</td>
        <td>{{ j.tier_actual }} {{ j.rank_actual }}</td>
        <td>{{ j.escalado }}</td>
        <td>+{{ j.bonus }}</td>
        <td>-{{ j.castigos }}</td>
        <td class="voz-ok">{{ j.tiempo_voz_min }} min</td>
        <td><span class="badge-shell">{{ j.escudos }} escudo(s)</span>{% if j.aegis_activo %}<span class="badge-aegis">Aegis {{ j.aegis_restante }}h</span>{% endif %}</td>
        <td><b>{{ j.total }}</b></td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <p class="vacio">Aun no hay jugadores en esta categoria.</p>
    {% endif %}
  </div>
  <div class="categoria">
    <h2>Low Elo</h2>
    {% if low %}
    <table>
      <tr><th>#</th><th>Jugador</th><th>Rango actual</th><th>Escalado</th><th>Bonus</th><th>Castigos</th><th>Voz</th><th>Escudos / Aegis</th><th>Total</th></tr>
      {% for j in low %}
      <tr>
        <td class="{{ 'pos1' if loop.index==1 else ('pos2' if loop.index==2 else ('pos3' if loop.index==3 else '')) }}">{{ loop.index }}</td>
        <td>{{ j.nombre }}</td>
        <td>{{ j.tier_actual }} {{ j.rank_actual }}</td>
        <td>{{ j.escalado }}</td>
        <td>+{{ j.bonus }}</td>
        <td>-{{ j.castigos }}</td>
        <td class="voz-ok">{{ j.tiempo_voz_min }} min</td>
        <td><span class="badge-shell">{{ j.escudos }} escudo(s)</span>{% if j.aegis_activo %}<span class="badge-aegis">Aegis {{ j.aegis_restante }}h</span>{% endif %}</td>
        <td><b>{{ j.total }}</b></td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <p class="vacio">Aun no hay jugadores en esta categoria.</p>
    {% endif %}
  </div>
  {% if sin_voz %}
  <div class="categoria">
    <h2>Sin verificar (falta chat de voz)</h2>
    <table>
      <tr><th>Jugador</th><th>Tiempo en voz</th></tr>
      {% for j in sin_voz %}
      <tr><td>{{ j.nombre }}</td><td class="voz-no">{{ j.tiempo_voz_min }} min</td></tr>
      {% endfor %}
    </table>
  </div>
  {% endif %}
  {% if pendientes %}
  <div class="categoria">
    <h2>Pendientes de clasificacion (Directiva)</h2>
    <table>
      <tr><th>Jugador</th><th>Rango actual</th><th>Elo previo declarado</th></tr>
      {% for j in pendientes %}
      <tr><td>{{ j.nombre }}</td><td>{{ j.tier_actual }} {{ j.rank_actual }}</td><td>{{ j.elo_previo or '-' }}</td></tr>
      {% endfor %}
    </table>
  </div>
  {% endif %}
</div>
<footer>Actualizado automaticamente - Pagina se refresca cada 60 segundos</footer>
</body>
</html>
"""


@app.route('/')
def home():
    db = cargar_db()
    high, low, pendientes, sin_voz = calcular_tabla(db)
    return render_template_string(PAGINA_HTML, high=high, low=low, pendientes=pendientes, sin_voz=sin_voz,
                                   duracion=DURACION_TORNEO, estado_torneo=calcular_estado_torneo(db),
                                   premio=PREMIO_GANADOR_USD)


@app.route('/api/tabla')
def api_tabla():
    db = cargar_db()
    high, low, pendientes, sin_voz = calcular_tabla(db)
    return jsonify({'high': high, 'low': low, 'pendientes': pendientes, 'sin_voz': sin_voz,
                     'estado_torneo': calcular_estado_torneo(db)})


Thread(target=lambda: app.run(host='0.0.0.0', port=10000), daemon=True).start()

client.run(DISCORD_TOKEN)
