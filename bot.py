import discord
from discord import app_commands
from discord.ext import tasks
import requests
import json
import os
import random
import datetime
import asyncio
import signal
import sys
import time
from threading import Thread, Lock
from flask import Flask, jsonify, render_template_string

HTTP_SESSION = requests.Session()  # sesion HTTP reutilizable (keep-alive): acelera las llamadas a la API de Riot

# ================= CONFIGURACIÓN =================
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
RIOT_API_KEY = os.environ.get('RIOT_API_KEY')
CANAL_CLASIFICACION_ID = int(os.environ.get('CANAL_CLASIFICACION_ID', '0'))
CANAL_MALDICIONES_ID = int(os.environ.get('CANAL_MALDICIONES_ID', '0'))
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
MALDICION_MAX_ACTIVAS = 3           # limite base (puesto 3 en adelante) de maldiciones activas por victima
MALDICION_MAX_ACTIVAS_TOP1 = 9      # limite especial para quien esta en el puesto 1
MALDICION_MAX_ACTIVAS_TOP2 = 6      # limite especial para quien esta en el puesto 2
MALDICION_DURACION_HORAS = 24
ESCUDOS_MAX_INVENTARIO = 3          # maximo de Escudos Azules que un jugador puede acumular
CASTIGOS_PENDIENTES_PARA_AEGIS = 3  # castigos SIN cumplir recibidos que activan el Aegis
AEGIS_DURACION_HORAS = 12           # proteccion temporal tras activar el Aegis
POSTPARTIDA_GRACIA_MINUTOS = 10     # minutos tras terminar una partida en los que no se puede lanzar
TORNEO_BLOQUEO_FINAL_HORAS = 48     # ultimas horas del torneo en las que el sistema Blue Shell se desactiva
DROP_DIARIO_INTERVALO_MIN = 10      # frecuencia de revision de partidas para escudos automaticos
COOLDOWN_RECEPCION_HORAS = 12       # cooldown fijo (para todos los puestos) antes de poder volver a maldecir a alguien
ALERTA_INCUMPLIMIENTO_HORAS = 24    # si un castigo lleva mas de esto sin cumplirse, se avisa a la Directiva y al jugador


def cooldown_recepcion_horas(posicion):
        """Cooldown (horas) antes de poder volver a maldecir a alguien: fijo para todos los puestos,
        incluido el puesto 1 (ya no tiene excepcion de 'sin cooldown')."""
        return 0  # cooldown eliminado: los castigos se acumulan hasta el maximo por puesto


def maldicion_max_activas_por_posicion(posicion):
        """Maximo de maldiciones activas que puede acumular un jugador a la vez, segun su puesto en la tabla:
        Puesto 1: hasta 9. Puesto 2: hasta 6. Resto (o sin clasificar): el limite base."""
        if posicion == 1:
            return MALDICION_MAX_ACTIVAS_TOP1
        if posicion == 2:
            return MALDICION_MAX_ACTIVAS_TOP2
        return MALDICION_MAX_ACTIVAS


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

# Gama completa de campeones (nombre para mostrar). Las maldiciones de tipo campeon
# se sortean sobre TODO este pool, no sobre un subconjunto reducido.
CAMPEONES_POOL = [
    'Aatrox', 'Ahri', 'Akali', 'Akshan', 'Alistar', 'Amumu', 'Anivia', 'Annie', 'Aphelios', 'Ashe',
    'Aurelion Sol', 'Azir', 'Bard', "Bel'Veth", 'Blitzcrank', 'Brand', 'Braum', 'Briar', 'Caitlyn',
    'Camille', 'Cassiopeia', "Cho'Gath", 'Corki', 'Darius', 'Diana', 'Dr. Mundo', 'Draven', 'Ekko',
    'Elise', 'Evelynn', 'Ezreal', 'Fiddlesticks', 'Fiora', 'Fizz', 'Galio', 'Gangplank', 'Garen',
    'Gnar', 'Gragas', 'Graves', 'Gwen', 'Hecarim', 'Heimerdinger', 'Hwei', 'Illaoi', 'Irelia',
    'Ivern', 'Janna', 'Jarvan IV', 'Jax', 'Jayce', 'Jhin', 'Jinx', "K'Sante", "Kai'Sa", 'Kalista',
    'Karma', 'Karthus', 'Kassadin', 'Katarina', 'Kayle', 'Kayn', 'Kennen', "Kha'Zix", 'Kindred',
    'Kled', "Kog'Maw", 'LeBlanc', 'Lee Sin', 'Leona', 'Lillia', 'Lissandra', 'Lucian', 'Lulu', 'Lux',
    'Malphite', 'Malzahar', 'Maokai', 'Master Yi', 'Milio', 'Miss Fortune', 'Mordekaiser', 'Morgana',
    'Naafiri', 'Nami', 'Nasus', 'Nautilus', 'Neeko', 'Nidalee', 'Nilah', 'Nocturne', 'Nunu & Willump',
    'Olaf', 'Orianna', 'Ornn', 'Pantheon', 'Poppy', 'Pyke', 'Qiyana', 'Quinn', 'Rakan', 'Rammus',
    "Rek'Sai", 'Rell', 'Renata Glasc', 'Renekton', 'Rengar', 'Riven', 'Rumble', 'Ryze', 'Samira',
    'Sejuani', 'Senna', 'Seraphine', 'Sett', 'Shaco', 'Shen', 'Shyvana', 'Singed', 'Sion', 'Sivir',
    'Skarner', 'Smolder', 'Sona', 'Soraka', 'Swain', 'Sylas', 'Syndra', 'Tahm Kench', 'Taliyah',
    'Talon', 'Taric', 'Teemo', 'Thresh', 'Tristana', 'Trundle', 'Tryndamere', 'Twisted Fate',
    'Twitch', 'Udyr', 'Urgot', 'Varus', 'Vayne', 'Veigar', "Vel'Koz", 'Vex', 'Vi', 'Viego', 'Viktor',
    'Vladimir', 'Volibear', 'Warwick', 'Wukong', 'Xayah', 'Xerath', 'Xin Zhao', 'Yasuo', 'Yone',
    'Yorick', 'Yuumi', 'Zac', 'Zed', 'Zeri', 'Ziggs', 'Zilean', 'Zoe', 'Zyra', 'Ambessa', 'Aurora',
    'Mel',
]

# Excepciones de nombre -> id de Data Dragon para campeones cuyo id no se puede derivar
# simplemente quitando espacios y apostrofes del nombre mostrado.
CAMPEON_ID_ESPECIAL = {
    'Wukong': 'MonkeyKing',
    'Renata Glasc': 'Renata',
    'Nunu & Willump': 'Nunu',
    'Dr. Mundo': 'DrMundo',
    "Kai'Sa": 'Kaisa',
    "Kha'Zix": 'Khazix',
    "Vel'Koz": 'Velkoz',
    "Cho'Gath": 'Chogath',
    "K'Sante": 'KSante',
    "Bel'Veth": 'Belveth',
    'LeBlanc': 'Leblanc',
}


def icono_campeon(nombre):
    n = CAMPEON_ID_ESPECIAL.get(nombre) or nombre.replace("'", "").replace(" ", "")
    return f'https://ddragon.leagueoflegends.com/cdn/{DDRAGON_VERSION}/img/champion/{n}.png'


def splash_campeon(nombre):
    """Arte de fondo (splash art) del campeon, para decorar la web. Mismo CDN gratuito de Riot."""
    n = CAMPEON_ID_ESPECIAL.get(nombre) or nombre.replace("'", "").replace(" ", "")
    return f'https://ddragon.leagueoflegends.com/cdn/img/champion/splash/{n}_0.jpg'


_CAMPEON_ID_A_NOMBRE = {}


def _cargar_mapa_campeones():
    """Carga (una sola vez, en memoria) el mapa championId numerico -> nombre, usando Data Dragon
    (gratuito, sin API key). Se necesita porque Champion Mastery v4 solo devuelve IDs numericos."""
    global _CAMPEON_ID_A_NOMBRE
    if _CAMPEON_ID_A_NOMBRE:
        return
    try:
        r = HTTP_SESSION.get(f'https://ddragon.leagueoflegends.com/cdn/{DDRAGON_VERSION}/data/en_US/champion.json', timeout=8)
        data = r.json().get('data', {})
        for info in data.values():
            _CAMPEON_ID_A_NOMBRE[int(info['key'])] = info['name']
    except Exception:
        pass


async def top_3_campeones_mas_jugados(puuid, region):
    """Consulta la API gratuita Champion Mastery v4 de Riot para saber los 3 campeones con mas
    maestria de un jugador (proxy fiable de 'mas jugados', ya que la maestria crece con las partidas).
    Devuelve una lista de nombres, o None si no se pudo obtener."""
    plataforma = PLATFORM_MAP.get((region or 'lan').lower())
    if not plataforma:
        return None
    try:
        _cargar_mapa_campeones()
        url = f'https://{plataforma}.api.riotgames.com/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/top?count=3'
        r = HTTP_SESSION.get(url, headers={'X-Riot-Token': RIOT_API_KEY}, timeout=8)
        if r.status_code != 200:
            return None
        nombres = []
        for entry in r.json():
            nombre = _CAMPEON_ID_A_NOMBRE.get(entry.get('championId'))
            if nombre:
                nombres.append(nombre)
        return nombres or None
    except Exception:
        return None


# Pares de hechizos de invocador posibles para la maldicion 'hechizos_cambiados'.
HECHIZOS_POSIBLES = [
    ('Flash', 'Ignite'), ('Flash', 'Exhaust'), ('Flash', 'Barrier'), ('Flash', 'Cleanse'),
    ('Ghost', 'Heal'), ('Ghost', 'Ignite'), ('Teleport', 'Cleanse'), ('Teleport', 'Ignite'),
    ('Exhaust', 'Ignite'), ('Heal', 'Barrier'), ('Smite', 'Flash'), ('Smite', 'Ignite'),
]

# Clases de campeon (tags oficiales de Riot) para la maldicion 'clase_aleatoria'.
CLASES_CAMPEONES = {
    'Tirador': ['Ashe', 'Caitlyn', 'Jinx', "Kai'Sa", 'Jhin', 'Vayne', 'Ezreal', 'Miss Fortune', 'Draven',
                'Twitch', 'Varus', 'Sivir', 'Xayah', 'Zeri', 'Samira', 'Aphelios', 'Smolder', "Kog'Maw", 'Tristana'],
    'Mago': ['Ahri', 'Annie', 'Lux', 'Syndra', 'Veigar', 'Orianna', 'Ziggs', 'Xerath', 'Brand', 'Vex',
             'Malzahar', 'Viktor', 'Ryze', 'Azir', 'Cassiopeia', 'Karthus', 'Zoe', 'Hwei', 'Swain', 'Anivia'],
    'Asesino': ['Zed', 'Akali', 'Talon', 'Katarina', 'Fizz', 'Kassadin', 'Qiyana', "Kha'Zix", 'Naafiri',
                'Ekko', 'Evelynn', 'Rengar', 'Nocturne', 'LeBlanc', 'Pyke'],
    'Luchador': ['Garen', 'Darius', 'Aatrox', 'Riven', 'Irelia', 'Jax', 'Camille', 'Fiora', 'Renekton',
                 'Wukong', 'Gnar', 'Gwen', 'Trundle', 'Sett', 'Yone', 'Yasuo', 'Volibear', 'Illaoi', 'Olaf', 'Urgot'],
    'Tanque': ['Malphite', 'Ornn', 'Sion', 'Maokai', 'Nautilus', 'Leona', 'Braum', 'Rammus', 'Amumu', 'Zac',
               'Sejuani', 'Shen', "Cho'Gath", 'Nunu & Willump', 'Rell', 'Alistar', 'Galio'],
    'Soporte': ['Soraka', 'Nami', 'Lulu', 'Janna', 'Karma', 'Yuumi', 'Milio', 'Renata Glasc', 'Bard',
                'Senna', 'Taric', 'Seraphine'],
}

# Los 10 castigos oficiales del torneo modelo (soloqchallenge.gg), con su probabilidad real de salir
# UNA VEZ que ya se descarto el Reverse (que se sortea aparte, segun la posicion del objetivo).
CASTIGOS_OFICIALES = [
    ('sin_3_campeones', 17, None),
    ('yuumi', 11, 'Debes jugar obligatoriamente **Yuumi** en tu proxima partida.'),
    ('campeon_aleatorio', 11, None),
    ('sin_flash', 11, 'No puedes llevar **Flash** como hechizo de invocador en tu proxima partida.'),
    ('autofill', 11, 'Debes entrar a cola en modo **Autofill** (posicion "Cualquiera", sin elegir rol principal) en tu proxima partida.'),
    ('sin_botas', 11, 'No puedes comprar **botas** (ningun tier) ni llevar la runa **Pies Veloces** en tu proxima partida.'),
    ('hechizos_cambiados', 6, None),
    ('sin_pociones_pinks', 6, 'No puedes comprar **pociones** ni **Control Wards (pinks)** en toda la partida.'),
    ('sin_objetos_min15', 6, 'No puedes completar ningun objeto **mitico/legendario** hasta el minuto 15.'),
    ('clase_aleatoria', 4, None),
]


def generar_efecto_maldicion(posicion_objetivo=None):
    """Devuelve un dict {tipo, texto, opciones, elegido} representando el efecto de la maldicion,
    replicando el sistema Blue Shell del torneo modelo: primero se sortea el Reverse segun la
    posicion del objetivo (tabla oficial, ver probabilidad_reverse), y si no sale, se sortea uno
    de los 10 castigos oficiales respetando sus probabilidades reales (CASTIGOS_OFICIALES)."""
    if random.random() < probabilidad_reverse(posicion_objetivo):
        return {
            'tipo': 'reverse',
            'texto': 'REVERSE: la maldicion rebota. El castigo lo cumple quien la lanzo, no el objetivo.',
            'opciones': [], 'elegido': None,
        }

    tipos = [c[0] for c in CASTIGOS_OFICIALES]
    pesos = [c[1] for c in CASTIGOS_OFICIALES]
    tipo = random.choices(tipos, weights=pesos, k=1)[0]

    if tipo == 'sin_3_campeones':
        # El texto definitivo (con los nombres reales) se completa en /maldecir, que si puede
        # consultar de forma asincrona la maestria de campeones del jugador afectado via la API de Riot.
        return {'tipo': tipo, 'texto': 'No puedes jugar tus **3 campeones mas jugados de este SoloQ** en tu proxima partida (la Directiva los verifica en tu historial de SoloQ).',
                'opciones': [], 'elegido': None}
    if tipo == 'campeon_aleatorio':
        campeon = random.choice(CAMPEONES_POOL)
        return {'tipo': tipo, 'texto': f'Debes jugar obligatoriamente a **{campeon}** en tu proxima partida.',
                'opciones': [campeon], 'elegido': campeon}
    if tipo == 'hechizos_cambiados':
        par = random.choice(HECHIZOS_POSIBLES)
        return {'tipo': tipo, 'texto': f'Hechizos de invocador obligatorios: solo **{par[0]} + {par[1]}** en tu proxima partida.',
                'opciones': [], 'elegido': None}
    if tipo == 'clase_aleatoria':
        clase = random.choice(list(CLASES_CAMPEONES.keys()))
        ejemplos = ', '.join(random.sample(CLASES_CAMPEONES[clase], min(4, len(CLASES_CAMPEONES[clase]))))
        return {'tipo': tipo, 'texto': f'Debes jugar un campeon de la clase **{clase}** en tu proxima partida (ej: {ejemplos}).',
                'opciones': [], 'elegido': None}

    texto = next(c[2] for c in CASTIGOS_OFICIALES if c[0] == tipo)
    return {'tipo': tipo, 'texto': texto, 'opciones': [], 'elegido': None}


# Sesiones de voz activas en memoria: {discord_id: datetime_de_ultimo_checkpoint}
VOICE_SESIONES = {}


# ------------------- PERSISTENCIA (Google Sheets, con cache en memoria) -------------------
# La API de Google Sheets tiene limites de cuota (por defecto ~60 escrituras/min por proyecto).
# El diseno anterior hacia una lectura + una reescritura COMPLETA de la hoja en cada comando de
# Discord, lo que ademas de arriesgar el limite de cuota bajo uso concurrente, anadia latencia de
# red a cada comando. Ahora la base de datos completa vive en memoria (_db_cache/_registros_cache):
# los comandos leen y escriben sobre esa cache (instantaneo) y un task en segundo plano vuelca los
# cambios pendientes a Sheets cada FLUSH_INTERVALO_SEG segundos, sin importar cuantos comandos se
# hayan ejecutado mientras tanto. Para acciones destructivas o muy poco frecuentes (reiniciar
# registro, iniciar torneo) se fuerza un volcado inmediato para no arriesgar esos cambios.

GOOGLE_SHEET_NAME = os.environ.get('GOOGLE_SHEET_NAME', 'SoloQ Challenge DB')
GOOGLE_CREDENTIALS_JSON = os.environ.get('GOOGLE_CREDENTIALS_JSON', '')
FLUSH_INTERVALO_SEG = int(os.environ.get('FLUSH_INTERVALO_SEG', '20'))

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

_cache_lock = Lock()
_db_cache = None; _db_cargado_ok = False
_db_dirty = False
_registros_cache = None; _registros_cargados_ok = False
_registros_dirty = False
_tabla_cache = None
_tabla_cache_ts = 0.0
_tabla_cache_lock = Lock()
TABLA_CACHE_TTL_SEG = int(os.environ.get('TABLA_CACHE_TTL_SEG', '45'))

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


def _con_reintentos(func, intentos=3, espera_base=2):
    """Ejecuta func() reintentando ante errores transitorios de la API de Sheets (rate limit,
    fallos de red). Sin esto, un simple 429 de Google tiraba el guardado entero."""
    ultimo_error = None
    for intento in range(intentos):
        try:
            return func()
        except Exception as e:
            ultimo_error = e
            if intento < intentos - 1:
                time.sleep(espera_base * (intento + 1))
    raise ultimo_error


def _cargar_db_desde_sheets():
    """Lectura real desde Google Sheets. Solo se invoca una vez, al arrancar el bot (o si la
    cache en memoria aun no existe)."""
    try:
        ws = _get_or_create_worksheet('jugadores', JUGADORES_HEADERS)
        filas = _con_reintentos(lambda: ws.get_all_records(numericise_ignore=['all']))
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
        for fila in _con_reintentos(lambda: meta_ws.get_all_records(numericise_ignore=['all'])):
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
        raise


def _guardar_db_en_sheets(db):
    """Escritura real a Google Sheets. Solo la invoca el flush en segundo plano (o el flush
    forzado de acciones criticas), nunca directamente cada comando."""
    try:
        ws = _get_or_create_worksheet('jugadores', JUGADORES_HEADERS)
        filas = [JUGADORES_HEADERS]
        for puuid, data in jugadores_validos(db).items():
            fila = [puuid] + [_valor_a_texto(data.get(campo, '')) for campo in JUGADORES_HEADERS[1:]]
            filas.append(fila)
        _con_reintentos(lambda: ws.clear())
        _con_reintentos(lambda: ws.update('A1', filas, value_input_option='RAW'))

        meta_ws = _get_or_create_worksheet('meta', META_HEADERS)
        meta_filas = [META_HEADERS,
                      ['inicio_torneo', _valor_a_texto(db.get('inicio_torneo', ''))],
                      ['torneo_iniciado', 'True' if db.get('torneo_iniciado') else 'False'],
                      ['drop_diario_activo', 'True' if db.get('drop_diario_activo') else 'False'],
                      ['reto_activo_texto', _valor_a_texto(db.get('reto_activo_texto', ''))],
                      ['reto_activo_fecha', _valor_a_texto(db.get('reto_activo_fecha', ''))]]
        _con_reintentos(lambda: meta_ws.clear())
        _con_reintentos(lambda: meta_ws.update('A1', meta_filas, value_input_option='RAW'))
        return True
    except Exception as e:
        print(f'Error guardando DB en Google Sheets: {e}')
        return False


def cargar_db():
    """Devuelve la base de datos desde la cache en memoria (instantaneo, sin red). Solo golpea
    la API de Sheets la primera vez que se llama, al arrancar el bot."""
    global _db_cache, _db_cargado_ok
    with _cache_lock:
        if _db_cache is None:
            _db_cache = _cargar_db_desde_sheets(); _db_cargado_ok = True
        return _db_cache


def guardar_db(db, forzar=False):
    """Actualiza la cache en memoria y marca los cambios como pendientes de sincronizar con
    Sheets (los recoge el flush periodico). Con forzar=True (reiniciar_registro, iniciar_torneo)
    se sincroniza con Sheets de inmediato porque son acciones raras/criticas que no deben
    arriesgarse a perderse si el bot se reinicia antes del proximo flush."""
    global _db_cache, _db_dirty
    with _cache_lock:
        _db_cache = db
        _db_dirty = True
    if forzar:
        flush_db_sincrono()


def flush_db_sincrono():
    """Vuelca la cache actual de jugadores a Sheets de forma bloqueante. Se usa desde el task
    periodico (en un hilo aparte) y desde las acciones con forzar=True."""
    global _db_dirty
    with _cache_lock:
        db_actual = _db_cache
    if db_actual is None or not _db_cargado_ok or not _db_dirty:
        return
    if _guardar_db_en_sheets(db_actual):
        with _cache_lock:
            _db_dirty = False


def _cargar_registros_desde_sheets():
    try:
        ws = _get_or_create_worksheet('registros', REGISTROS_HEADERS)
        filas = _con_reintentos(lambda: ws.get_all_records(numericise_ignore=['all']))
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
        raise


def _guardar_registros_en_sheets(registros):
    try:
        ws = _get_or_create_worksheet('registros', REGISTROS_HEADERS)
        filas = [REGISTROS_HEADERS]
        for r in registros:
            filas.append([_valor_a_texto(r.get(campo, '')) for campo in REGISTROS_HEADERS])
        _con_reintentos(lambda: ws.clear())
        _con_reintentos(lambda: ws.update('A1', filas, value_input_option='RAW'))
        return True
    except Exception as e:
        print(f'Error guardando registros en Google Sheets: {e}')
        return False


def cargar_registros():
    global _registros_cache, _registros_cargados_ok
    with _cache_lock:
        if _registros_cache is None:
            _registros_cache = _cargar_registros_desde_sheets(); _registros_cargados_ok = True
        return _registros_cache


def guardar_registros(registros):
    global _registros_cache, _registros_dirty
    with _cache_lock:
        _registros_cache = registros
        _registros_dirty = True


def flush_registros_sincrono():
    global _registros_dirty
    with _cache_lock:
        actuales = _registros_cache
    if actuales is None or not _registros_cargados_ok or not _registros_dirty:
        return
    if _guardar_registros_en_sheets(actuales):
        with _cache_lock:
            _registros_dirty = False


async def flush_pendientes():
    """Vuelca a Sheets (en hilos aparte, sin bloquear el bot) los cambios de jugadores y/o
    registros que esten pendientes. La llama el task periodico y el apagado del servicio."""
    with _cache_lock:
        hay_db = _db_dirty
        hay_registros = _registros_dirty
    if hay_db:
        await asyncio.to_thread(flush_db_sincrono)
    if hay_registros:
        await asyncio.to_thread(flush_registros_sincrono)


def flush_total_sincrono():
    """Version bloqueante de flush_pendientes, para usar al apagar el proceso (senal SIGTERM de
    Render en cada redeploy), donde ya no hay tiempo de esperar un hilo aparte."""
    flush_db_sincrono()
    flush_registros_sincrono()


@tasks.loop(seconds=FLUSH_INTERVALO_SEG)
async def sincronizar_sheets():
    try:
        await flush_pendientes()
    except Exception as e:
        print(f'Error en sincronizacion periodica con Sheets: {e}')


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
    """riot_id con formato 'Nombre#TAG'. Usa account-v1 + league-v4 by-puuid.
    Cualquier fallo de red/API (timeout, SSL, etc.) devuelve None en vez de propagar la excepcion,
    para que un problema puntual con la API de Riot no tumbe toda la pagina/tabla."""
    plataforma = PLATFORM_MAP.get(region.lower())
    region_base = REGION_MAP.get(region.lower())
    if not plataforma or not region_base or '#' not in riot_id:
        return None
    game_name, tag_line = riot_id.split('#', 1)
    headers = {'X-Riot-Token': RIOT_API_KEY}

    try:
        url = f'https://{region_base}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}'
        r = HTTP_SESSION.get(url, headers=headers, timeout=8)
        if r.status_code != 200:
            return None
        cuenta = r.json()
        puuid = cuenta['puuid']
        nombre_completo = f"{cuenta['gameName']}#{cuenta['tagLine']}"

        url2 = f'https://{plataforma}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}'
        r2 = HTTP_SESSION.get(url2, headers=headers, timeout=8)
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
    except Exception as e:
        print(f'Error obteniendo info ranked de {riot_id}: {e}')
        return None



def esta_en_partida_activa(puuid, plataforma):
    """Spectator API: True si el jugador tiene una partida en vivo en este momento.
    Nota: la API publica de Riot no expone cola/seleccion de campeon, solo partidas ya iniciadas."""
    try:
        headers = {'X-Riot-Token': RIOT_API_KEY}
        url = f'https://{plataforma}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}'
        r = HTTP_SESSION.get(url, headers=headers, timeout=6)
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
        r = HTTP_SESSION.get(url, headers=headers, timeout=6)
        if r.status_code != 200 or not r.json():
            return False
        for match_id in r.json():
            url2 = f'https://{region_base}.api.riotgames.com/lol/match/v5/matches/{match_id}'
            r2 = HTTP_SESSION.get(url2, headers=headers, timeout=6)
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
        if False:  # (desactivado) ya no hay espera de 10 min tras terminar partida
            return f'termino una partida hace menos de {POSTPARTIDA_GRACIA_MINUTOS} minutos (se esta procesando el resultado)'
    except Exception:
        return None

def canal_maldiciones():    
    """Canal dedicado donde se publican los resultados de /maldecir y /castigar, para no ensuciar
    otros canales del servidor. Si no esta configurado (CANAL_MALDICIONES_ID), usa el canal de
    clasificacion como respaldo."""
    if CANAL_MALDICIONES_ID != 0:
        canal = client.get_channel(CANAL_MALDICIONES_ID)
        if canal:
            return canal
    if CANAL_CLASIFICACION_ID != 0:
        return client.get_channel(CANAL_CLASIFICACION_ID)
    return None

async def enviar_dm_seguro(discord_id, texto):
    """Intenta enviar un mensaje directo (DM) a un jugador. Si tiene los DMs cerrados o ya no esta
    en el servidor, falla en silencio: nunca debe romper el flujo del comando que lo llama."""
    try:
        guild = client.get_guild(int(DISCORD_GUILD_ID))
        if not guild:
            return
        member = guild.get_member(int(discord_id))
        if not member:
            return
        await member.send(texto)
    except Exception:
        pass

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

def discord_tag_de(discord_id):
    """Nombre de usuario/tag de Discord del jugador (ej. 'usuario' o 'usuario#1234'), leido desde
    la cache en memoria del cliente de discord.py (member cache del servidor). No hace ninguna
    llamada de red, es una simple lectura de cache, por lo que es seguro llamarla desde cualquier
    hilo (incluyendo las rutas sincronas de Flask). Devuelve '' si el bot todavia no tiene cacheado
    a ese miembro (por ejemplo, si aun no ha terminado de conectar) o si ya no esta en el servidor."""
    try:
        guild = client.get_guild(int(DISCORD_GUILD_ID))
        if not guild:
            return ''
        member = guild.get_member(int(discord_id))
        if not member:
            return ''
        return str(member)
    except Exception:
        return ''


def en_voz_ahora(discord_id):
    """True si el jugador esta conectado AHORA a un canal de voz del servidor (lectura de cache, sin red)."""
    try:
        guild = client.get_guild(int(DISCORD_GUILD_ID))
        if not guild:
            return False
        member = guild.get_member(int(discord_id))
        return bool(member and member.voice and member.voice.channel)
    except Exception:
        return False


def calcular_tabla(db):
    """Devuelve (high, low, pendientes, sin_voz) con los datos ya frescos de Riot.
    El orden dentro de cada categoria se basa en 'escalado' (division/liga), no solo LP crudo."""
    global _tabla_cache, _tabla_cache_ts
    with _tabla_cache_lock:
        if _tabla_cache is not None and (time.time() - _tabla_cache_ts) < TABLA_CACHE_TTL_SEG:
            return _tabla_cache
    high, low, pendientes, sin_voz = [], [], [], []
    for puuid, data in jugadores_validos(db).items():
        info = obtener_info_ranked(data['nombre'], data['region'])
        if info is None:
            continue
        lp_ganados = info['lp'] - data['lp_inicial']
        escalado_inicial = valor_escalado(data.get('tier_inicial', info['tier']), data.get('rank_inicial', ''), data['lp_inicial'])
        escalado_actual = valor_escalado(info['tier'], info['rank'], info['lp'])
        progreso_escalado = escalado_actual - escalado_inicial
        # El orden de la tabla se basa en el rango/LP actual (escalado_actual), no en el progreso.
        total = escalado_actual + data.get('bonus_total', 0) - data.get('castigos_total', 0)
        tiempo_voz = round(data.get('tiempo_voz_min', 0), 1)
        pendientes_castigo = castigos_pendientes_de(data)
        wins = info.get('wins', 0)
        losses = info.get('losses', 0)
        partidas = wins + losses
        winrate = round((wins / partidas) * 100) if partidas else 0
        avatar_campeon = icono_campeon(CAMPEONES_POOL[hash(puuid) % len(CAMPEONES_POOL)])
        game_name, _, tag_line = data['nombre'].partition('#')
        opgg_url = f'https://www.op.gg/summoners/lan/{game_name}-{tag_line}' if tag_line else ''
        jugador = {
            'puuid': puuid,
            'discord_id': data['discord_id'],
            'discord_tag': discord_tag_de(data['discord_id']),
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
            'en_voz': en_voz_ahora(data['discord_id']),
            'maldiciones_lista': [{'texto': m.get('texto', ''), 'cumplido': bool(m.get('cumplido')), 'icono': (icono_campeon(m.get('elegido')) if m.get('elegido') else '')} for m in maldiciones_activas_de(data)],
            'elo_previo': data.get('elo_previo', ''),
            'escudos': data.get('escudos', 0),
            'aegis_activo': aegis_activo(data),
            'aegis_restante': round(aegis_restante_horas(data), 1),
            'castigos_pendientes': len(pendientes_castigo),
            'wins': wins,
            'losses': losses,
            'partidas': partidas,
            'winrate': winrate,
            'racha': data.get('racha_victorias', 0),
            'avatar': avatar_campeon,
            'opgg_url': opgg_url,
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
    resultado = (high, low, pendientes, sin_voz)
    with _tabla_cache_lock:
        _tabla_cache = resultado
        _tabla_cache_ts = time.time()
    return resultado

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
    info = await asyncio.to_thread(obtener_info_ranked, nombre, region)
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
            info = await asyncio.to_thread(obtener_info_ranked, data['nombre'], data['region'])
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
    high, low, pendientes, sin_voz = await asyncio.to_thread(calcular_tabla, db)

    objetivo = None
    for j in high + low + pendientes + sin_voz:
        if j['discord_id'] == user_id:
            objetivo = j
            break
    if objetivo is None:
        await interaction.followup.send('No estas registrado. Usa `/registrar` primero.')
        return
    posicion = None
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
    max_activas_perfil = maldicion_max_activas_por_posicion(posicion)
    malds_txt = '\n'.join(f'- {m["texto"]}' + (f' | lanzada por <@{m["de"]}>' if m.get('de') else '') + (' (cumplido)' if m.get('cumplido') else ' (pendiente)') for m in activas) or 'Ninguna'
    embed.add_field(name=f'Maldiciones activas ({len(activas)}/{max_activas_perfil})', value=malds_txt, inline=False)
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
               f'Con `/maldecir @jugador` gastas uno para lanzar un castigo aleatorio: el mismo sistema y probabilidades '
               f'del torneo modelo (ver campo 4b mas abajo). Maximo de maldiciones activas por victima segun su puesto: '
               f'Puesto 1 hasta {MALDICION_MAX_ACTIVAS_TOP1}, Puesto 2 hasta {MALDICION_MAX_ACTIVAS_TOP2}, resto hasta '
               f'{MALDICION_MAX_ACTIVAS}. Duran {MALDICION_DURACION_HORAS}h cada una.'),
        inline=False)
    embed.add_field(
        name='4b. Los 10 castigos posibles (probabilidad real, tras descartar Reverse)',
        value=('Sin tus 3 campeones mas jugados **17%** - Yuumi obligatorio **11%** - Campeon aleatorio obligatorio **11%** - '
               'Sin Flash **11%** - Autofill/sin rol principal **11%** - Sin botas ni Pies Veloces **11%** - Hechizos '
               'cambiados **6%** - Sin pociones ni pinks **6%** - Sin objetos miticos hasta min 15 **6%** - Clase de '
               'campeon aleatoria (tirador/mago/asesino/luchador/tanque/soporte) **4%**.'),
        inline=False)
    embed.add_field(
        name='5. Cooldown de recepcion',
        value=f'Eliminado: las maldiciones se acumulan sin espera hasta el maximo del puesto. Al llenarse el cupo se activa un Aegis de {AEGIS_DURACION_HORAS}h.',
        inline=False)
    embed.add_field(
        name='6. Reverse',
        value=('Cuanto mas abajo este tu objetivo, mas probable es que la shell rebote y el castigo lo cumplas tu. '
               'Top1: 1% - Top2: 2% - Top3: 3% - Top4: 4% - Top5: 5% - Resto: 15%. Tirar hacia arriba es mas seguro. '
               'Si sale reverse no tienes que hacer nada: rebota sola y el castigo se sortea para quien la lanzo.'),
        inline=False)
    embed.add_field(
        name='7. Restricciones de lanzamiento',
        value=('No puedes lanzar una maldicion si estas en una partida en vivo (ya no hay espera tras terminar una partida). '
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
        value=(f'Si un jugador llena su maximo de maldiciones activas segun su puesto (9/6/3), se activa '
               f'automaticamente un Aegis de {AEGIS_DURACION_HORAS}h que lo protege de nuevas maldiciones.'),
        inline=False)
    embed.add_field(
        name='11. Cumplimiento de castigos',
        value=('Se cumple en la siguiente partida posible. Excepciones: si ya habias aceptado la cola cuando llego, o si es '
               'imposible cumplirlo (ej. te toca un campeon baneado), se cumple en la siguiente que puedas. Prohibido sabotear '
               'tu propio castigo para volverlo imposible. Jugar partidas ignorando un castigo pendiente es incumplir la norma. '
               'No tienes que marcar nada: la Directiva revisa y marca como cumplido con `/cumplir_castigo` en un plazo razonable. '
               f'Si un castigo lleva mas de {ALERTA_INCUMPLIMIENTO_HORAS}h sin marcarse como cumplido, el bot avisa '
               'automaticamente a la Directiva y por DM al jugador para que se verifique.'),
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
        value=('Efecto aleatorio que recibe un jugador cuando alguien usa `/maldecir` contra el. Es uno de los 10 '
               'castigos oficiales del torneo modelo (sin tus 3 campeones mas jugados, Yuumi obligatorio, campeon '
               'aleatorio, sin Flash, Autofill, sin botas, hechizos cambiados, sin pociones/pinks, sin objetos '
               'miticos hasta min 15, o clase de campeon aleatoria) o un Reverse. Usa `/reglamento` para ver el '
               'listado completo con los porcentajes reales. Es diferente de un "castigo manual" (`/castigar`), '
               'aunque ambos restan puntos o imponen una condicion.'),
        inline=False)
    embed.add_field(
        name='Pendiente / Cumplido',
        value=('Estado de una maldicion. Queda **pendiente** hasta que la Directiva confirma en partida que se '
               'ejecuto y la marca como **cumplida** con `/cumplir_castigo`. Las pendientes cuentan para activar el Aegis.'),
        inline=False)
    embed.add_field(
        name='Cooldown de recepcion',
        value=f'Ya no existe: se pueden recibir maldiciones seguidas hasta llenar el maximo del puesto; al llenarse se activa el Aegis de {AEGIS_DURACION_HORAS}h.',
        inline=False)
    embed.add_field(
        name='Reverse',
        value='Rebote de la maldicion hacia quien la lanzo. La probabilidad depende de la posicion del objetivo (1% a 15%, mas seguro tirar hacia arriba).',
        inline=False)
    embed.add_field(
        name='Aegis (proteccion)',
        value=(f'Escudo TEMPORAL distinto del Escudo Azul: se activa automaticamente por {AEGIS_DURACION_HORAS}h cuando '
               f'un jugador llena su maximo de maldiciones activas segun su puesto (9/6/3). Mientras dura, nadie '
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
        name='High Elo / Low Elo',
        value='Categorias del torneo asignadas manualmente por la Directiva con `/clasificar` tras revisar la cuenta.',
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
    high, low, pendientes, sin_voz = await asyncio.to_thread(calcular_tabla, db)

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
               f'Usa `/maldecir` para gastar uno. Maximo de maldiciones activas por victima segun su puesto (Puesto 1: '
               f'{MALDICION_MAX_ACTIVAS_TOP1}, Puesto 2: {MALDICION_MAX_ACTIVAS_TOP2}, resto: {MALDICION_MAX_ACTIVAS}). '
               f'Sin cooldown de recepcion: los castigos se acumulan hasta el maximo del puesto y al llenarse se activa el Aegis. '
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
    _, _, pend, _ = await asyncio.to_thread(calcular_tabla, db)
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
            cd_txt = f'{cd_horas}h'
            max_activas = maldicion_max_activas_por_posicion(pos)
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
                f'Maldiciones activas sobre ti ({len(activas)}/{max_activas} - limite segun tu puesto):\n{malds_txt}'
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
    motivo_bloqueo = await asyncio.to_thread(motivo_bloqueo_por_partida, caster_puuid, caster_data)
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

    pos_destino = posicion_de_jugador(db, destino_puuid)
    max_activas_destino = maldicion_max_activas_por_posicion(pos_destino)
    if len(maldiciones_activas_de(destino_data)) >= max_activas_destino:
        await interaction.followup.send(
            f'**{destino_data["nombre"]}** ya tiene el maximo de {max_activas_destino} maldiciones activas ahora mismo. '
            f'Intenta con otro objetivo o espera a que expiren (dura {MALDICION_DURACION_HORAS}h).')
        return

    if False:  # (desactivado) la maestria de Riot mezcla todas las colas, no solo SoloQ, y salia mal la info
        top3 = await top_3_campeones_mas_jugados(destino_puuid, destino_data.get('region'))
        if top3:
            efecto['texto'] = f'No puedes jugar ninguno de tus 3 campeones mas jugados en tu proxima partida: **{", ".join(top3)}**.'
        else:
            efecto['texto'] = 'No puedes jugar tus 3 campeones mas jugados en tu proxima partida (revisa tu maestria de campeones en el cliente de LoL para saber cuales son).'

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
    if len(maldiciones_activas_de(destino_data)) >= max_activas_destino:
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
    if efecto['tipo'] == 'campeon_aleatorio':
        embed.add_field(name='Efecto', value=efecto['texto'], inline=False)
        embed.set_thumbnail(url=icono_campeon(efecto['elegido']))
    elif efecto['tipo'] == 'yuumi':
        embed.add_field(name='Efecto', value=efecto['texto'], inline=False)
        embed.set_thumbnail(url=icono_campeon('Yuumi'))
    else:
        embed.add_field(name='Efecto', value=efecto['texto'], inline=False)
        cd_destino_txt = 'sin cooldown de recepcion'
        embed.set_footer(text=f'Dura {MALDICION_DURACION_HORAS}h - Maximo {max_activas_destino} activas por jugador - El objetivo original tenia {cd_destino_txt}')
    canal_destino = canal_maldiciones()
    if canal_destino:
        await canal_destino.send(
            content=f'<@{destino_data["discord_id"]}> te lanzaron una maldicion Blue Shell!',
            embed=embed)
        if canal_destino.id != interaction.channel_id:
            await interaction.followup.send(f'Maldicion lanzada. Revisa {canal_destino.mention} para el detalle.')
    else:
        await interaction.followup.send(
            content=f'<@{destino_data["discord_id"]}> te lanzaron una maldicion Blue Shell!',
            embed=embed)
    await enviar_dm_seguro(
        destino_data['discord_id'],
        f'Te lanzaron una maldicion Blue Shell en SoloQ Challenge: {efecto["texto"]}\nDura {MALDICION_DURACION_HORAS}h. Revisa el canal de maldiciones para mas detalles.'
    )
    if aegis_otorgado:
        aegis_msg = (f'<@{destino_data["discord_id"]}> llenaste tu cupo de {max_activas_destino} maldiciones activas: '
                     f'se activo tu **Aegis** (proteccion) por {AEGIS_DURACION_HORAS}h. Nadie podra maldecirte mientras dure.')
        if canal_destino:
            await canal_destino.send(aegis_msg)
        else:
            await interaction.followup.send(aegis_msg)

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
    guardar_db({}, forzar=True)
    await interaction.followup.send(
        'Se borraron todos los registros. Todos deben usar `/registrar` de nuevo con sus cuentas nuevas '
        '(pueden usar `elo_previo` para que la directiva sepa su nivel real al revisar).')


@tree.command(name='eliminar_registro', description='(Directiva) Elimina el registro de UN jugador para que pueda registrarse de nuevo')
@app_commands.describe(usuario='Jugador cuyo registro se va a borrar', confirmar='Escribe SI (mayusculas) para confirmar el borrado')
async def eliminar_registro(interaction: discord.Interaction, usuario: discord.Member, confirmar: str):
    if not await requiere_directiva(interaction):
        return
    await interaction.response.defer()
    if confirmar != 'SI':
        await interaction.followup.send(
            'Accion cancelada. Escribe `confirmar: SI` (en mayusculas) para confirmar el borrado del registro de '
            f'{usuario.mention} (se pierde su LP, bonus, castigos, logros, escudos y tiempo de voz acumulado de esa cuenta).')
        return
    db = cargar_db()
    for puuid, data in list(jugadores_validos(db).items()):
        if data.get('discord_id') == str(usuario.id):
            nombre_borrado = data.get('nombre', '?')
            del db[puuid]
            guardar_db(db, forzar=True)
            await interaction.followup.send(
                f'Se borro el registro de **{nombre_borrado}** (<@{usuario.id}>). '
                f'Ya puede usar `/registrar` de nuevo con su nueva cuenta.')
            return
    await interaction.followup.send(f'{usuario.mention} no tiene ningun registro activo en el torneo.')


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
        info = await asyncio.to_thread(obtener_info_ranked, data['nombre'], data['region'])
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
    guardar_db(db, forzar=True)
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
            texto_castigo = f'<@{usuario.id}> recibiste un castigo de **-{puntos} puntos**.\nMotivo: {motivo}\nTotal castigos: -{data["castigos_total"]}'
            canal_destino = canal_maldiciones()
            if canal_destino:
                await canal_destino.send(texto_castigo)
                if canal_destino.id != interaction.channel_id:
                    await interaction.followup.send(f'Castigo aplicado. Revisa {canal_destino.mention} para el detalle.')
            else:
                await interaction.followup.send(texto_castigo)
            await enviar_dm_seguro(
                str(usuario.id),
                f'Recibiste un castigo en SoloQ Challenge: -{puntos} puntos.\nMotivo: {motivo}'
            )
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


def _procesar_partida_jugador(puuid, data, validos, headers):
    """Revisa la ultima partida ranked del jugador y otorga Escudos Azules automaticos segun corresponda.
    Devuelve una lista de textos de anuncio (puede estar vacia) y modifica 'data' in-place."""
    anuncios = []
    region_base = REGION_MAP.get(data.get('region', 'lan').lower())
    if not region_base:
        return anuncios
    try:
        url = f'https://{region_base}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?count=1'
        r = HTTP_SESSION.get(url, headers=headers, timeout=8)
        if r.status_code != 200 or not r.json():
            return anuncios
        match_id = r.json()[0]
        if match_id == data.get('ultimo_match_procesado'):
            return anuncios

        url2 = f'https://{region_base}.api.riotgames.com/lol/match/v5/matches/{match_id}'
        r2 = HTTP_SESSION.get(url2, headers=headers, timeout=8)
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
                r3 = HTTP_SESSION.get(url3, headers=headers, timeout=8)
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
        anuncios = await asyncio.to_thread(_procesar_partida_jugador, puuid, data, validos, headers)
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
@tasks.loop(minutes=30)
async def revisar_incumplimientos():
    """Revisa si algun castigo (maldicion) lleva mas de ALERTA_INCUMPLIMIENTO_HORAS sin marcarse como
    cumplido. Si es asi, avisa UNA sola vez (queda marcado para no repetir el aviso) en el canal de
    maldiciones, mencionando a la Directiva, y por mensaje directo al jugador afectado."""
    db = cargar_db()
    cambios = False
    avisos = []
    for puuid, data in jugadores_validos(db).items():
        for m in data.get('maldiciones', []) or []:
            if m.get('cumplido') or m.get('alertado_incumplimiento'):
                continue
            try:
                fecha = datetime.datetime.fromisoformat(m['fecha'])
            except Exception:
                continue
            horas = (datetime.datetime.now() - fecha).total_seconds() / 3600
            if horas < ALERTA_INCUMPLIMIENTO_HORAS:
                continue
            m['alertado_incumplimiento'] = True
            cambios = True
            avisos.append((data['discord_id'], data['nombre'], m.get('texto', ''), round(horas, 1)))
    if cambios:
        guardar_db(db)
    if not avisos:
        return
    canal = canal_maldiciones()
    guild = client.get_guild(int(DISCORD_GUILD_ID))
    rol = discord.utils.get(guild.roles, name=ROL_DIRECTIVA_NOMBRE) if guild else None
    mencion_rol = rol.mention if rol else f'@{ROL_DIRECTIVA_NOMBRE}'
    for discord_id, nombre, texto_castigo, horas in avisos:
        mensaje = (f'{mencion_rol} **{nombre}** (<@{discord_id}>) lleva mas de {round(horas)}h sin que se '
                   f'marque como cumplido su castigo: "{texto_castigo}". Verifiquen con `/cumplir_castigo`.')
        if canal:
            try:
                await canal.send(mensaje)
            except Exception:
                pass
        await enviar_dm_seguro(
            discord_id,
            f'Recordatorio de SoloQ Challenge: tu maldicion "{texto_castigo}" lleva mas de {round(horas)}h sin '
            'marcarse como cumplida. Si ya la cumpliste, pidele a la Directiva que la confirme con /cumplir_castigo.'
        )

@tasks.loop(minutes=5)
async def voice_checkpoint():
    for discord_id in list(VOICE_SESIONES.keys()):
        flush_voice_time(discord_id)
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
        if not revisar_incumplimientos.is_running():
            revisar_incumplimientos.start()
    if not revisar_partidas_recientes.is_running():
        revisar_partidas_recientes.start()
    if not sincronizar_sheets.is_running():
        sincronizar_sheets.start()


# ------------------- SITIO WEB (gratis, mismo servicio) -------------------

app = Flask(__name__)

TIER_COLORES_WEB = {
    'IRON': '#5b504a', 'BRONZE': '#8c5a3c', 'SILVER': '#9fa8b3', 'GOLD': '#e0a83e',
    'PLATINUM': '#3fc1b0', 'EMERALD': '#3fae6a', 'DIAMOND': '#5aa7f5', 'MASTER': '#b463e6',
    'GRANDMASTER': '#e5484d', 'CHALLENGER': '#f5c518', 'UNRANKED': '#6b7280',
}

# Emblemas oficiales de rango (Community Dragon: espejo publico y gratuito de los
# assets de Riot, igual que Data Dragon, sin necesidad de API key).
RANK_EMBLEMA_BASE = 'https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-static-assets/global/default/images/ranked-emblem/emblem-{}.png'


def emblema_rango(tier):
    t = (tier or 'unranked').lower()
    if t not in ('iron', 'bronze', 'silver', 'gold', 'platinum', 'emerald', 'diamond', 'master', 'grandmaster', 'challenger'):
        return ''
    return RANK_EMBLEMA_BASE.format(t)


# Datos del servidor de Discord del torneo, para dar credito en la web (footer + credits).
DISCORD_INVITE_URL = 'https://discord.gg/UHz8hTaETN'
DISCORD_GUILD_ID = '331997851355709451'
DISCORD_GUILD_NOMBRE = 'ｓｃａｒｙ💞Ｌ♡ ｖｅ'
# Logo oficial del servidor (imagen fija enviada por la directiva), alojado gratis en
# i.imgur.com (subida anonima, sin API key) para no depender de un asset en el repo.
DISCORD_GUILD_ICON = 'https://i.imgur.com/PaDbmn0.png'
DISCORD_GUILD_BANNER = f'https://cdn.discordapp.com/banners/{DISCORD_GUILD_ID}/f6e8674ea85341845e5e3dd8fc48a45a.png?size=1024'

PAGINA_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>SoloQ Challenge</title>
<meta property="og:title" content="SoloQ Challenge - Torneo LAN">
<meta property="og:description" content="Torneo interno de escalado de rango en League of Legends. Blue Shells, Escudos Azules, Aegis y mucho mas. Un proyecto de {{ discord_nombre }}.">
{% if fondo_url %}<meta property="og:image" content="{{ fondo_url }}">{% endif %}
{% if icono_destacado %}<link rel="icon" href="{{ icono_destacado }}">{% endif %}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>
  :root { --acc:#ff2d78; --acc-dim:#c9105a; --bg:#0c0510; --panel:#160a14; --panel2:#1e0d1a; --line:#3a1229; --muted:#a888a0; }
  * { box-sizing: border-box; }
  .icon { width:1em; height:1em; display:inline-block; vertical-align:-0.15em; stroke:currentColor; fill:none; stroke-width:1.7; stroke-linecap:round; stroke-linejoin:round; }
  .uc { font-family:'Inter',sans-serif; text-transform:uppercase; letter-spacing:1.5px; }
  .disp { font-family:'Anton','Inter',sans-serif; font-weight:400; letter-spacing:0.5px; }
  #inicio, #escudos, #tabla { scroll-margin-top:74px; }
  .navbar { position:sticky; top:0; z-index:30; background:#000000f0; backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px); border-bottom:1px solid var(--line); }
  .navbar-inner { max-width:1250px; margin:0 auto; padding:0 20px; display:flex; align-items:center; justify-content:space-between; height:64px; gap:16px; overflow-x:auto; }
  .navbar-marca { display:flex; align-items:center; gap:10px; color:#fff; font-family:'Anton',sans-serif; font-weight:400; font-size:1.15em; letter-spacing:0.5px; white-space:nowrap; }
  .navbar-marca .icon { width:24px; height:24px; color:var(--acc); }
  .navbar-chip { display:inline-flex; align-items:center; gap:5px; background:var(--panel2); border:1px solid var(--line); color:#d1d1d6; padding:6px 12px; border-radius:7px; font-size:0.78em; font-weight:700; text-transform:uppercase; letter-spacing:0.5px; white-space:nowrap; }
  .navbar-links { display:flex; gap:6px; white-space:nowrap; align-items:center; }
  .navbar-links a { display:inline-flex; align-items:center; gap:6px; color:#9a9aa2; text-decoration:none; font-family:'Inter',sans-serif; font-weight:600; letter-spacing:0.2px; font-size:0.9em; padding:8px 14px; border-radius:7px; transition: color .2s, background .2s; }
  .navbar-links a:hover { color:#fff; }
  .navbar-links a.activo { background:var(--acc); color:#0a0a0c; font-weight:700; }
  .navbar-links a .icon { width:14px; height:14px; }
  .navbar-right { display:flex; align-items:center; gap:12px; }
  .navbar-countdown { display:flex; gap:6px; font-family:'Anton',sans-serif; font-size:0.95em; color:#fff; white-space:nowrap; }
  .navbar-countdown span { color:var(--acc); }
  .eyebrow { text-transform:uppercase; letter-spacing:3px; color:var(--acc); font-family:'Inter',sans-serif; font-weight:700; font-size:0.85em; opacity:.9; }
  .btn-outline { display:inline-flex; align-items:center; gap:8px; margin-top:22px; padding:12px 30px; background:var(--acc); border:1px solid var(--acc); color:#0a0a0c; text-decoration:none; font-family:'Inter',sans-serif; font-weight:800; text-transform:uppercase; letter-spacing:1.5px; font-size:0.92em; transition: filter .2s; cursor:pointer; border-radius:8px; }
  .btn-outline:hover { filter:brightness(1.1); }
  .medal-badge { width:30px; height:30px; border-radius:8px; display:flex; align-items:center; justify-content:center; font-family:'Anton',sans-serif; font-weight:400; font-size:1em; border:1.5px solid currentColor; background:#0e1117; }
  .medal-badge.p1 { color:var(--acc); box-shadow:0 0 14px rgba(255,45,120,.35); }
  .medal-badge.p2 { color:#c0c0c0; }
  .medal-badge.p3 { color:#cd7f32; }
  .icon-circle { color:var(--acc); display:inline-flex; flex-shrink:0; margin-top:2px; }
  .icon-circle .icon { width:18px; height:18px; }
  .avatar { width:38px; height:38px; border-radius:50%; object-fit:cover; border:1.5px solid var(--line); flex-shrink:0; }
  .avatar.big { width:52px; height:52px; border:2px solid var(--line); }
  .flame { width:1em; height:1em; display:inline-block; vertical-align:-0.12em; color:#ff6a3d; }
  .lp-fila { display:flex; align-items:baseline; gap:6px; }
  .lp-num { font-family:'Anton',sans-serif; font-weight:400; }
  .wr-bar { width:100%; height:6px; border-radius:4px; background:#ed4245; overflow:hidden; display:flex; }
  .wr-bar .win { height:100%; background:var(--acc); }
  .delta-up { color:var(--acc); font-weight:700; }
  .delta-down { color:#ed4245; font-weight:700; }
  .racha-badge { display:inline-flex; align-items:center; gap:4px; padding:3px 9px; border-radius:6px; background:#1c2414; color:var(--acc); font-weight:700; font-size:0.85em; border:1px solid #2e3a1c; }
  .racha-badge .icon { width:12px; height:12px; }
  .jugador-fila { display:flex; align-items:center; gap:10px; }
  .jugador-fila .nombres { line-height:1.25; }
  .jugador-fila .principal { font-weight:700; color:#fff; font-size:0.95em; }
  .jugador-fila .tag { color:#8a8a92; font-size:0.72em; }
  .btn-opgg { display:inline-flex; align-items:center; justify-content:center; padding:6px 14px; background:var(--panel2); border:1px solid var(--line); color:#e5e5ea; text-decoration:none; border-radius:6px; font-size:0.78em; font-weight:700; letter-spacing:0.5px; transition:border-color .2s, color .2s; }
  .btn-opgg:hover { border-color:var(--acc); color:var(--acc); }
  .buscador { display:flex; align-items:center; gap:8px; background:var(--panel2); border:1px solid var(--line); border-radius:8px; padding:8px 14px; color:#e5e5ea; }
  .buscador input { background:transparent; border:none; outline:none; color:#e5e5ea; font-family:'Inter',sans-serif; font-size:0.9em; width:170px; }
  .buscador input::placeholder { color:var(--muted); }
  .buscador .icon { width:15px; height:15px; color:var(--muted); }
  .filtros-fila { display:flex; flex-wrap:wrap; gap:10px; align-items:center; justify-content:space-between; margin:18px 0 4px; }
  .filtros-izq { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
  .badge-castigos { display:inline-flex; align-items:center; gap:6px; padding:8px 14px; background:var(--panel2); border:1px solid var(--line); border-radius:8px; color:#e5e5ea; font-size:0.85em; font-weight:600; }
  .badge-castigos b { color:#ed4245; }
  @keyframes brillo {
    0%, 100% { text-shadow: 0 0 18px rgba(245,197,24,0.55), 0 0 2px rgba(245,197,24,0.9); }
    50% { text-shadow: 0 0 34px rgba(245,197,24,0.95), 0 0 6px rgba(245,197,24,1); }
  }
  @keyframes flotar {
    0%, 100% { transform: translateY(0px) rotate(-4deg); }
    50% { transform: translateY(-10px) rotate(4deg); }
  }
  @keyframes girar { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
  @keyframes pulso {
    0%, 100% { box-shadow: 0 0 0 0 rgba(245,197,24,0.5); }
    50% { box-shadow: 0 0 0 10px rgba(245,197,24,0); }
  }
  @keyframes aparecer { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
  @keyframes subir {
    0% { transform: translateY(110vh) scale(1); opacity: 0; }
    8% { opacity: 0.7; }
    92% { opacity: 0.5; }
    100% { transform: translateY(-10vh) scale(0.5); opacity: 0; }
  }
  @keyframes barrido { 0% { background-position: -200px 0; } 100% { background-position: 200px 0; } }
  @keyframes desfile { 0% { transform: translateX(0); } 100% { transform: translateX(-50%); } }
  html { scroll-behavior: smooth; }
  body {
    background: var(--bg); color:#e8e8ea; font-family:'Inter','Segoe UI',Arial,sans-serif;
    margin:0; padding:0 0 60px; position:relative; min-height:100vh;
  }
  body::after {
    content:""; position:fixed; inset:0; pointer-events:none; z-index:0;
    background: radial-gradient(circle at 10% 0%, rgba(255,45,120,0.06), transparent 40%),
                radial-gradient(circle at 90% 100%, rgba(255,45,120,0.05), transparent 40%),
                radial-gradient(ellipse at 50% 50%, transparent 55%, rgba(0,0,0,0.55) 100%);
  }
  .particulas { position:fixed; inset:0; overflow:hidden; z-index:0; pointer-events:none; }
  .particulas span {
    position:absolute; bottom:-10px; border-radius:50%; background:var(--acc);
    box-shadow:0 0 6px 1px rgba(255,45,120,0.7); animation-name:subir; animation-iteration-count:infinite;
    animation-timing-function:linear;
  }
  h1, h2, h3 { font-family:'Anton', 'Segoe UI', sans-serif; font-weight:400; }
  header {
    position: relative; overflow: hidden; padding:56px 20px 30px; text-align:center;
    border-bottom:1px solid var(--line); z-index:1;
    background: linear-gradient(rgba(10,10,12,0.85), rgba(10,10,12,0.97)), {% if fondo_url %}url('{{ fondo_url }}'){% endif %};
    background-size: cover; background-position: center 20%;
  }
  header::before {
    content: ""; position: absolute; inset: 0;
    background: radial-gradient(circle at 50% -10%, rgba(255,45,120,0.16), transparent 60%);

    pointer-events: none;
  }
  .hex-corner { position:absolute; width:52px; height:52px; opacity:0.55; z-index:1; }
  .hex-corner.tl { top:12px; left:12px; }
  .hex-corner.tr { top:12px; right:12px; transform:scaleX(-1); }
  .hex-corner.bl { bottom:12px; left:12px; transform:scaleY(-1); }
  .hex-corner.br { bottom:12px; right:12px; transform:scale(-1,-1); }
  .logo-fila { display:flex; align-items:center; justify-content:center; gap:16px; position:relative; z-index:1; }
  .logo-escudo { font-size: 2.4em; color:var(--acc); display:inline-block; animation: flotar 3.5s ease-in-out infinite; filter: drop-shadow(0 0 8px rgba(255,45,120,0.5)); }
  .logo-escudo .icon { stroke-width:1.4; }
  header h1 { margin:0; font-size:3.2em; color:#fff; letter-spacing:1px; animation: brillo 2.6s ease-in-out infinite; }
  header p.subtitulo { color:#a3a3ab; margin-top:10px; position:relative; z-index:1; font-size:1.05em; letter-spacing:0.5px; }
  .estado-fila { display:flex; flex-wrap:wrap; gap:10px; justify-content:center; margin-top:18px; position:relative; z-index:1; }
  .estado { display:inline-block; padding:8px 18px; background:#18181bcc; border:1px solid var(--acc); border-radius:8px; color:var(--acc); font-weight:600; font-size:0.9em; animation: pulso 2.4s infinite; }
  .premio { display:inline-block; padding:8px 18px; background:#132a1ccc; border:1px solid #3ba55d; border-radius:8px; color:#3ba55d; font-weight:600; font-size:0.9em; }
  .countdown { display:inline-flex; align-items:center; gap:6px; padding:8px 18px; background:#18181bcc; border:1px solid var(--line); border-radius:8px; color:#e5e5ea; font-weight:600; font-size:0.9em; }
  .stats { display:flex; justify-content:center; gap:16px; margin-top:26px; flex-wrap:wrap; position:relative; z-index:1; }
  .stat-card {
    background:#141416cc; backdrop-filter: blur(2px); border-radius:10px; padding:14px 22px; min-width:120px;
    text-align:center; border:1px solid var(--line); transition: transform 0.2s ease, border-color 0.2s ease;
    animation: aparecer .5s ease-out both;
  }
  .stat-card:nth-child(1) { animation-delay: .05s; }
  .stat-card:nth-child(2) { animation-delay: .15s; }
  .stat-card:nth-child(3) { animation-delay: .25s; }
  .stat-card:hover { transform: translateY(-4px); border-color:var(--acc); }
  .stat-card .num { font-size:1.8em; color:var(--acc); font-weight:400; font-family:'Anton',sans-serif; }
  .stat-card .label { font-size:0.72em; color:#9a9aa2; text-transform:uppercase; letter-spacing:1px; }
  .destacado { display:flex; align-items:center; justify-content:center; gap:10px; margin-top:20px; position:relative; z-index:1; color:#9a9aa2; font-size:0.85em; }
  .destacado img { width:34px; height:34px; border-radius:50%; border:2px solid var(--acc); animation: girar 6s linear infinite; }
  .contenedor { max-width:1150px; margin:0 auto; padding:0 20px; position:relative; z-index:1; }
  .divisor { display:flex; align-items:center; gap:12px; margin:38px 0 20px; }
  .divisor::before, .divisor::after { content:""; flex:1; height:1px; background:linear-gradient(90deg, transparent, #f5c51899, transparent); }
  .divisor span { color:#f5c518; font-size:1.1em; }
  .aviso { background:#141416cc; border-left:4px solid var(--acc); padding:14px 18px; border-radius:6px; margin-top:24px; color:#d1d1d6; display:flex; gap:10px; align-items:flex-start; }
  .tabs { display:inline-flex; gap:4px; margin:8px 0 0; background:var(--panel2); padding:4px; border-radius:9px; }
  .tab-btn {
    display:inline-flex; align-items:center; gap:8px;
    background:transparent; border:none; color:#9a9aa2; padding:8px 22px;
    border-radius:6px; cursor:pointer; font-family:'Inter',sans-serif; font-weight:700;
    text-transform:uppercase; font-size:0.85em; letter-spacing:1px; transition: all .2s;
  }
  .tab-btn:hover { color:#fff; }
  .tab-btn.activo { background:var(--acc); color:#0a0a0c; }
  .tab-panel { display:none; }
  .tab-panel.activo { display:block; animation: aparecer .4s ease-out; }
  .categoria { margin-bottom:10px; background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:22px 22px 26px; position:relative; overflow:hidden; }
  .categoria.con-arte { background-size:cover; background-position:center 30%; }
  .categoria.con-arte::before { content:""; position:absolute; inset:0; background:linear-gradient(160deg, rgba(10,10,12,0.94), rgba(10,10,12,0.9)); pointer-events:none; }
  .categoria.con-arte > * { position:relative; z-index:1; }
  .categoria h2 { border-left:4px solid var(--acc); padding-left:12px; font-size:1.3em; margin-top:0; letter-spacing:0.5px; }
  .categoria h2 .sub { color:#7a7a82; font-size:0.55em; font-family:'Inter',sans-serif; font-weight:600; margin-left:8px; letter-spacing:0; text-transform:none; }
  .podio { display:flex; justify-content:center; align-items:stretch; gap:16px; margin:18px 0 30px; flex-wrap:wrap; }
  .podio-card {
    background:var(--panel); border:1px solid var(--line); border-radius:14px;
    padding:20px 20px 18px; text-align:left; width:230px; position:relative; transition:transform .25s;
  }
  .podio-card:hover { transform: translateY(-6px); }
  .podio-card.p1 { border-color:var(--acc); box-shadow:0 0 30px rgba(255,45,120,.12); }
  .podio-card.p2 { }
  .podio-card.p3 { }
  .podio-cabecera { display:flex; align-items:center; justify-content:space-between; }
  .podio-jugador { display:flex; align-items:center; gap:10px; margin-top:10px; }
  .podio-card .nombre { font-family:'Inter',sans-serif; font-weight:700; font-size:1em; color:#fff; }
  .podio-card .subnombre { font-size:0.75em; color:#8a8a92; margin-top:1px; }
  .podio-card .pts { font-size:2.1em; margin-top:16px; }
  .podio-stats { display:flex; justify-content:space-between; margin-top:16px; gap:6px; }
  .podio-stats .pstat { text-align:left; }
  .podio-stats .pstat .v { font-weight:700; font-size:0.92em; color:#e5e5ea; }
  .podio-stats .pstat .l { font-size:0.68em; color:#8a8a92; text-transform:uppercase; letter-spacing:0.5px; margin-top:1px; }
  .rango-fila { display:flex; align-items:center; justify-content:flex-start; gap:6px; margin-top:8px; }
  .emblema-rango { width:26px; height:26px; object-fit:contain; filter:drop-shadow(0 0 4px rgba(0,0,0,0.6)); }
  .emblema-rango.chico { width:20px; height:20px; }
  .tier-badge {
    display:inline-block; padding:2px 10px; border:1px solid #6b7280; border-radius:12px;
    font-size:0.72em; font-weight:700; letter-spacing:0.5px; white-space:nowrap;
  }
  .tabla-wrap { overflow-x:auto; }
  table { width:100%; border-collapse:collapse; background:var(--panel); border-radius:8px; overflow:hidden; min-width:820px; }
  th, td { padding:12px 14px; text-align:left; border-bottom:1px solid var(--line); }
  th { background:var(--panel2); color:#9a9aa2; text-transform:uppercase; font-size:0.72em; letter-spacing:1px; font-weight:700; }
  tr { transition: background 0.15s ease; }
  tr:hover { background:#1c1c1f; }
  .pos1 { color:var(--acc); font-weight:800; }
  .pos2 { color:#c0c0c0; font-weight:800; }
  .pos3 { color:#cd7f32; font-weight:800; }
  .voz-ok { color:#3ba55d; }
  .voz-no { color:#ed4245; }
  .vacio { color:#6b7280; padding:24px; text-align:center; }
  .badge-aegis { display:inline-block; padding:2px 8px; border-radius:10px; font-size:0.72em; font-weight:700; background:#132a1c; color:#3ba55d; border:1px solid #3ba55d; }
  .badge-shell { display:inline-block; padding:2px 8px; border-radius:10px; font-size:0.72em; font-weight:700; background:#22240f; color:var(--acc); border:1px solid #3a3d1c; margin-left:4px; }
  .barra { width:90px; height:8px; border-radius:6px; background:#20242e; overflow:hidden; }
  .barra-fill { height:100%; border-radius:6px; transition: width .6s ease; background-size: 40px 100%; background-image: linear-gradient(90deg, rgba(255,255,255,0.15) 25%, transparent 25%, transparent 50%, rgba(255,255,255,0.15) 50%, rgba(255,255,255,0.15) 75%, transparent 75%, transparent); animation: barrido 2s linear infinite; }
  .info-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }
  .info-card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:20px 16px; text-align:center; transition:transform .2s, border-color .2s; }
  .info-card:hover { transform: translateY(-3px); border-color:var(--acc); }
  .info-icono { width:44px; height:44px; margin:0 auto 12px; border-radius:50%; border:1.5px solid var(--acc); display:flex; align-items:center; justify-content:center; color:var(--acc); background:var(--panel2); }
  .info-icono .icon { width:22px; height:22px; }
  .titulo-card { font-family:'Inter',sans-serif; text-transform:uppercase; letter-spacing:0.5px; font-weight:700; font-size:0.9em; color:#d1d1d6; }
  .galeria-wrap { overflow:hidden; margin:6px 0 4px; -webkit-mask-image:linear-gradient(90deg, transparent, #000 8%, #000 92%, transparent); mask-image:linear-gradient(90deg, transparent, #000 8%, #000 92%, transparent); }
  .galeria-fila { display:flex; gap:16px; width:max-content; animation: desfile 40s linear infinite; }
  .galeria-fila img { width:56px; height:56px; border-radius:50%; border:2px solid #2a2f3a; opacity:0.65; transition: opacity .2s, border-color .2s, transform .2s; }
  .galeria-fila img:hover { opacity:1; border-color:#f5c518; transform: scale(1.12); }
  footer { text-align:center; color:#9a9aa2; margin-top:44px; font-size:0.85em; position:relative; z-index:1; }
  footer .divisor { max-width:400px; margin-left:auto; margin-right:auto; }
  .creditos {
    max-width:640px; margin:0 auto; padding:26px 24px; border-radius:14px; border:1px solid var(--line);
    position:relative; overflow:hidden; background:var(--panel);
  }
  .creditos.con-banner { background-size:cover; background-position:center; }
  .creditos.con-banner::before { content:""; position:absolute; inset:0; background:linear-gradient(180deg, rgba(10,10,12,0.55), rgba(10,10,12,0.93)); }
  .creditos > * { position:relative; z-index:1; }
  .creditos-servidor { display:flex; flex-direction:column; align-items:center; justify-content:center; gap:12px; margin-bottom:14px; text-align:center; }
  .creditos-servidor img { width:104px; height:104px; border-radius:50%; border:3px solid var(--acc); box-shadow:0 0 26px rgba(255,45,120,0.35); }
  .creditos-servidor .nombre { color:var(--acc); font-weight:400; font-size:1.2em; font-family:'Anton',sans-serif; }
  .btn-discord {
    display:inline-flex; align-items:center; gap:8px; margin-top:10px; padding:10px 22px;
    background:#5865F2; color:#fff; text-decoration:none; border-radius:22px; font-weight:700;
    font-family:'Inter',sans-serif; letter-spacing:0.3px; transition: transform .2s, box-shadow .2s;
  }
  .btn-discord:hover { transform: translateY(-2px); box-shadow:0 6px 18px rgba(88,101,242,0.45); }
  .creditos-riot { margin-top:16px; font-size:0.78em; color:#6b7280; }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation-duration:0.01ms !important; animation-iteration-count:1 !important; transition-duration:0.01ms !important; }
  }
  @media (max-width: 700px) {
    header h1 { font-size:2.1em; letter-spacing:1px; }
    .logo-escudo { font-size:1.7em; }
    .stat-card { min-width:92px; padding:10px 14px; }
    .stat-card .num { font-size:1.3em; }
    table { font-size:0.82em; min-width:640px; }
    th, td { padding:8px 8px; }
    .podio-card { width:100%; padding:16px; }
    .hex-corner { width:34px; height:34px; }
    .galeria-fila img { width:44px; height:44px; }
    .navbar-links { display:none; }
    .navbar-countdown { display:none; }
  }
  .voz-dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:4px; vertical-align:middle; }
  .voz-dot.on { background:#3aff8f; box-shadow:0 0 8px #3aff8f; }
  .voz-dot.off { background:#5a4a55; }
  .castigos-celda { display:flex; align-items:center; gap:4px; flex-wrap:wrap; max-width:180px; }
  .castigo-chip { display:inline-flex; align-items:center; gap:2px; background:var(--panel2); border:1px solid var(--line); border-radius:6px; padding:2px 4px; cursor:help; }
  .castigo-chip img { width:20px; height:20px; border-radius:4px; display:block; }
  .castigo-chip .icon { width:15px; height:15px; color:var(--acc); }
  .castigo-chip.cumplido { border-color:#3aff8f; }
  .castigo-chip.cumplido b { color:#3aff8f; font-size:0.62em; }
  .castigo-num { font-family:'Anton',sans-serif; color:var(--acc); font-size:1.05em; margin-left:2px; }
</style>
</head>
<body>
<nav class="navbar">
  <div class="navbar-inner">
    <div class="navbar-marca">
      <svg class="icon" viewBox="0 0 24 24"><path d="M12 2l7 3v6c0 5-3.5 8.5-7 10-3.5-1.5-7-5-7-10V5l7-3z"/></svg>
      SoloQ Challenge
    </div>
    <span class="navbar-chip">SQC 2026</span>
    <div class="navbar-links">
      <a href="#tabla" class="activo"><svg class="icon" viewBox="0 0 24 24"><path d="M8 4h8v4a4 4 0 0 1-8 0V4z"/><path d="M8 5H4v2a4 4 0 0 0 4 4M16 5h4v2a4 4 0 0 1-4 4"/><path d="M12 12v4M9 20h6M10 16h4v4h-4v-4z"/></svg>Ranking</a>
      <a href="#escudos"><svg class="icon" viewBox="0 0 24 24"><path d="M12 2c1 3-2 4-2 7a4 4 0 1 0 8 0c0-2-1-3-1-5 2 1 3 4 3 7a6 6 0 1 1-12 0c0-4 2-6 4-9z"/></svg>Escudos Azules</a>
      <a href="#inicio"><svg class="icon" viewBox="0 0 24 24"><path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/></svg>Inicio</a>
    </div>
    <div class="navbar-right">
      {% if fin_torneo_iso %}<div class="navbar-countdown disp"><span id="countdown-nav"></span></div>{% endif %}
      <a href="{{ discord_invite }}" target="_blank" rel="noopener" class="navbar-chip" style="background:#5865F2; border-color:#5865F2; color:#fff;">
        <svg class="icon" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M20.317 4.369a19.79 19.79 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.058a.082.082 0 0 0 .031.056 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028 14.09 14.09 0 0 0 1.226-1.994.076.076 0 0 0-.041-.106 13.107 13.107 0 0 1-1.872-.892.077.077 0 0 1-.008-.128c.126-.094.252-.192.372-.291a.074.074 0 0 1 .077-.01c3.927 1.793 8.18 1.793 12.061 0a.073.073 0 0 1 .078.01c.12.099.246.198.373.292a.077.077 0 0 1-.006.127c-.598.35-1.22.645-1.873.893a.076.076 0 0 0-.04.106c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.029 19.84 19.84 0 0 0 6.002-3.03.077.077 0 0 0 .032-.055c.5-5.177-.838-9.674-3.548-13.662a.06.06 0 0 0-.031-.028zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.955 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.946 2.418-2.157 2.418z"/></svg>
        Discord
      </a>
    </div>
  </div>
</nav>
<div class="particulas">
{% for p in particulas %}
<span style="left:{{ p.left }}%; width:{{ p.size }}px; height:{{ p.size }}px; animation-duration:{{ p.dur }}s; animation-delay:-{{ p.delay }}s;"></span>
{% endfor %}
</div>
<header id="inicio">
  <svg class="hex-corner tl" viewBox="0 0 100 100"><polygon points="50,3 96,26 96,74 50,97 4,74 4,26" fill="none" stroke="#f5c518" stroke-width="3"/><polygon points="50,22 78,36 78,64 50,78 22,64 22,36" fill="none" stroke="#9b59b6" stroke-width="1.5"/></svg>
  <svg class="hex-corner tr" viewBox="0 0 100 100"><polygon points="50,3 96,26 96,74 50,97 4,74 4,26" fill="none" stroke="#f5c518" stroke-width="3"/><polygon points="50,22 78,36 78,64 50,78 22,64 22,36" fill="none" stroke="#9b59b6" stroke-width="1.5"/></svg>
  <div class="logo-fila">
    <span class="logo-escudo"><svg class="icon" viewBox="0 0 24 24"><path d="M12 2l7 3v6c0 5-3.5 8.5-7 10-3.5-1.5-7-5-7-10V5l7-3z"/></svg></span>
    <div>
      <div class="eyebrow">Torneo Oficial</div>
      <h1>SoloQ Challenge</h1>
    </div>
    <span class="logo-escudo" style="animation-delay: -1.8s;"><svg class="icon" viewBox="0 0 24 24"><path d="M6 3h12l4 6-10 12L2 9l4-6z"/><path d="M2 9h20M8 3l4 6 4-6M6 9l6 12 6-12"/></svg></span>
  </div>
  <p class="subtitulo">Torneo interno de escalado de division/liga - {{ duracion }} dias - LAN</p>
  <div class="estado-fila">
    <div class="estado">{{ estado_torneo }}</div>
    <div class="premio">Premio: ${{ premio }} USD + insignias</div>
    {% if fin_torneo_iso %}<div class="countdown"><svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l4 2"/></svg><span id="countdown"></span></div>{% endif %}
  </div>
  <div class="stats">
    <div class="stat-card"><div class="num">{{ high|length + low|length }}</div><div class="label">Jugadores activos</div></div>
    <div class="stat-card"><div class="num">{{ pendientes|length }}</div><div class="label">Pendientes de clasificacion</div></div>
    <div class="stat-card"><div class="num">{{ sin_voz|length }}</div><div class="label">Sin verificar voz</div></div>
  </div>
  {% if campeon_destacado %}
  <div class="destacado"><img src="{{ icono_destacado }}" alt=""> Maldicion Blue Shell del momento: podria tocarte <b style="color:#c9a8e0">{{ campeon_destacado }}</b></div>
  {% endif %}
  <a href="#tabla" class="btn-outline">
    <svg class="icon" viewBox="0 0 24 24"><path d="M8 4h8v4a4 4 0 0 1-8 0V4z"/><path d="M8 5H4v2a4 4 0 0 0 4 4M16 5h4v2a4 4 0 0 1-4 4"/><path d="M12 12v4M9 20h6M10 16h4v4h-4v-4z"/></svg>
    Ver Clasificación
  </a>
</header>

{% if campeones_galeria %}
<div class="galeria-wrap">
  <div class="galeria-fila">
    {% for c in campeones_galeria %}<img loading="lazy" src="{{ c.icono }}" title="{{ c.nombre }}" alt="{{ c.nombre }}">{% endfor %}
    {% for c in campeones_galeria %}<img loading="lazy" src="{{ c.icono }}" title="{{ c.nombre }}" alt="{{ c.nombre }}">{% endfor %}
  </div>
</div>
{% endif %}

{% macro podio(lista) %}
{% if lista %}
{% set max_total = (lista[:3]|map(attribute='total')|list|max) %}
<div class="podio">
  {% for j in lista[:3] %}
  {% set pct = 0 %}
  {% if max_total and max_total > 0 %}{% set pct = (j.total / max_total * 100) %}{% endif %}
  <div class="podio-card p{{ loop.index }}">
    <div class="podio-cabecera">
      <div class="medal-badge {{ 'p1' if loop.index==1 else ('p2' if loop.index==2 else 'p3') }}">
        {% if loop.index == 1 %}<svg class="icon" viewBox="0 0 24 24"><path d="M4 8l4 3 4-6 4 6 4-3-2 10H6L4 8z"/><path d="M6 20h12"/></svg>{% else %}{{ loop.index }}{% endif %}
      </div>
      <a href="{{ j.opgg_url }}" target="_blank" rel="noopener" class="btn-opgg">OP.GG</a>
    </div>
    <div class="podio-jugador">
      <img loading="lazy" class="avatar big" src="{{ j.avatar }}" alt="">
      <div>
        <div class="nombre">{{ j.nombre.split('#')[0] }}</div>
        <div class="subnombre">{{ j.nombre }}{% if j.discord_tag %} · @{{ j.discord_tag }}{% endif %}</div>
      </div>
    </div>
    <div class="lp-fila pts">
      <svg class="icon flame" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M12 2c1 3-2 4-2 7a4 4 0 1 0 8 0c0-2-1-3-1-5 2 1 3 4 3 7a6 6 0 1 1-12 0c0-4 2-6 4-9z"/></svg>
      <span class="lp-num">{{ j.total }}</span><span style="font-size:0.5em; color:#8a8a92; font-weight:700;">PTS</span>
    </div>
    <div class="podio-stats">
      <div class="pstat"><div class="v">{{ j.wins }}W {{ j.losses }}L</div><div class="l">{{ j.partidas }} partidas</div></div>
      <div class="pstat"><div class="v">{{ j.winrate }}%</div><div class="l">Winrate</div></div>
      <div class="pstat"><div class="v"><span class="racha-badge">{{ j.racha }}<svg class="icon" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M12 2c1 3-2 4-2 7a4 4 0 1 0 8 0c0-2-1-3-1-5 2 1 3 4 3 7a6 6 0 1 1-12 0c0-4 2-6 4-9z"/></svg></span></div><div class="l">Racha</div></div>
    </div>
    <div class="barra" style="width:100%; margin-top:14px;"><div class="barra-fill" style="width:{{ pct|round(1) }}%; background:var(--acc);"></div></div>
  </div>
  {% endfor %}
</div>
{% endif %}
{% endmacro %}

{% macro tabla_jugadores(lista) %}
{% if lista %}
<div class="tabla-wrap">
<table class="tabla-jugadores">
  <tr><th>#</th><th>Jugador</th><th>Rango</th><th>Net Wins</th><th>Racha</th><th>Voz</th><th>Castigos</th><th>Escudos / Aegis</th><th>PTS</th><th>Stats</th></tr>
  {% for j in lista %}
  {% set emb = emblema_rango(j.tier_actual) %}
  <tr data-nombre="{{ j.nombre|lower }}">
    <td class="{{ 'pos1' if loop.index==1 else ('pos2' if loop.index==2 else ('pos3' if loop.index==3 else '')) }}">{{ loop.index }}</td>
    <td>
      <div class="jugador-fila">
        <img loading="lazy" class="avatar" src="{{ j.avatar }}" alt="">
        <div class="nombres">
          <div class="principal">{{ j.nombre.split('#')[0] }}</div>
          <div class="tag">#{{ j.nombre.split('#')[1] if '#' in j.nombre else '' }}{% if j.discord_tag %} · @{{ j.discord_tag }}{% endif %}</div>
        </div>
      </div>
    </td>
    <td>
      <div class="rango-fila" style="justify-content:flex-start;">
        {% if emb %}<img loading="lazy" class="emblema-rango chico" src="{{ emb }}" alt="{{ j.tier_actual }}">{% endif %}
        <span class="tier-badge" style="border-color:{{ tier_colors.get(j.tier_actual,'#6b7280') }}; color:{{ tier_colors.get(j.tier_actual,'#6b7280') }};">{{ j.tier_actual }} {{ j.rank_actual }} - {{ j.lp_actual }} LP</span>
      </div>
    </td>
    <td style="min-width:120px;">
      <div class="wr-bar"><div class="win" style="width:{{ j.winrate }}%;"></div></div>
      <div style="font-size:0.75em; color:#8a8a92; margin-top:3px;">{{ j.winrate }}% - {{ j.wins }}W {{ j.losses }}L</div>
    </td>
    <td><span class="racha-badge">{{ j.racha }}<svg class="icon" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M12 2c1 3-2 4-2 7a4 4 0 1 0 8 0c0-2-1-3-1-5 2 1 3 4 3 7a6 6 0 1 1-12 0c0-4 2-6 4-9z"/></svg></span></td>
    <td>{% if j.en_voz %}<span class="voz-dot on"></span> Si{% else %}<span class="voz-dot off"></span> No{% endif %}</td>
    <td>
      <div class="castigos-celda">
        {% for m in j.maldiciones_lista %}<span class="castigo-chip {{ 'cumplido' if m.cumplido else '' }}" title="{{ m.texto }}{{ ' - CUMPLIDA' if m.cumplido else ' - pendiente' }}">{% if m.icono %}<img loading="lazy" src="{{ m.icono }}" alt="">{% else %}<svg class="icon" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M12 2c1 3-2 4-2 7a4 4 0 1 0 8 0c0-2-1-3-1-5 2 1 3 4 3 7a6 6 0 1 1-12 0c0-4 2-6 4-9z"/></svg>{% endif %}{% if m.cumplido %}<b>OK</b>{% endif %}</span>{% endfor %}
        {% if j.maldiciones_lista %}<span class="castigo-num">{{ j.maldiciones_lista|length }}</span>{% else %}<span style="color:var(--muted);">0</span>{% endif %}
      </div>
    </td>
    <td><span class="badge-shell">{{ j.escudos }} escudo(s)</span>{% if j.aegis_activo %}<span class="badge-aegis">Aegis {{ j.aegis_restante }}h</span>{% endif %}</td>
    <td><b>{{ j.total }}</b></td>
    <td>{% if j.opgg_url %}<a href="{{ j.opgg_url }}" target="_blank" rel="noopener" class="btn-opgg">OP.GG</a>{% endif %}</td>
  </tr>
  {% endfor %}
</table>
</div>
{% else %}
<p class="vacio">Aun no hay jugadores en esta categoria.</p>
{% endif %}
{% endmacro %}

<div class="contenedor">
  <div class="aviso"><span class="icon-circle"><svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 11h1v6h1"/></svg></span><span>Para que tus puntos cuenten debes conectarte al chat de voz del Discord mientras juegas: la columna "Voz" muestra quien esta conectado ahora mismo. La tabla se ordena por PTS (escalado de division/liga). En "Castigos" ves las maldiciones activas de cada jugador: pasa el cursor sobre cada icono para leer el castigo; las marcadas con OK ya estan cumplidas.</span></div>

  <div id="tabla" class="filtros-fila">
    <div class="filtros-izq">
      <div class="tabs">
        <button class="tab-btn activo" onclick="mostrarTab('high', this)"><svg class="icon" viewBox="0 0 24 24"><path d="M8 4h8v4a4 4 0 0 1-8 0V4z"/><path d="M8 5H4v2a4 4 0 0 0 4 4M16 5h4v2a4 4 0 0 1-4 4"/><path d="M12 12v4M9 20h6M10 16h4v4h-4v-4z"/></svg>High Elo</button>
        <button class="tab-btn" onclick="mostrarTab('low', this)"><svg class="icon" viewBox="0 0 24 24"><path d="M4 4l16 16M20 4L4 20"/></svg>Low Elo</button>
      </div>
      <div class="buscador">
        <svg class="icon" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
        <input type="text" id="buscarJugador" placeholder="Buscar jugador" oninput="filtrarJugadores(this.value)">
      </div>
      {% if total_castigos_pendientes %}
      <div class="badge-castigos"><svg class="icon" viewBox="0 0 24 24"><path d="M12 2c1 3-2 4-2 7a4 4 0 1 0 8 0c0-2-1-3-1-5 2 1 3 4 3 7a6 6 0 1 1-12 0c0-4 2-6 4-9z"/></svg>Castigos <b>{{ total_castigos_pendientes }}</b></div>
      {% endif %}
    </div>
  </div>
  <div id="tab-high" class="tab-panel activo">
    <div class="categoria">
      <h2>High Elo <span class="sub">Master · GM · Challenger</span></h2>
      {{ podio(high) }}
      {{ tabla_jugadores(high) }}
    </div>
  </div>
  <div id="tab-low" class="tab-panel">
    <div class="categoria">
      <h2>Low Elo <span class="sub">Hierro - Diamante</span></h2>
      {{ podio(low) }}
      {{ tabla_jugadores(low) }}
    </div>
  </div>

  {% if sin_voz %}
  <div class="divisor"><span>◆</span></div>
  <div class="categoria">
    <h2>Sin verificar <span class="sub">falta chat de voz</span></h2>
    <div class="tabla-wrap">
    <table>
      <tr><th>Jugador</th><th>Voz</th></tr>
      {% for j in sin_voz %}
      <tr><td>{{ j.nombre }}</td><td>{% if j.en_voz %}<span class="voz-dot on"></span> Conectado (sumando minutos){% else %}<span class="voz-dot off"></span> No conectado{% endif %}</td></tr>
      {% endfor %}
    </table>
    </div>
  </div>
  {% endif %}

  {% if pendientes %}
  <div class="divisor"><span>◆</span></div>
  <div class="categoria">
    <h2>Pendientes de clasificacion <span class="sub">Directiva</span></h2>
    <div class="tabla-wrap">
    <table>
      <tr><th>Jugador</th><th>Rango actual</th><th>Elo previo declarado</th></tr>
      {% for j in pendientes %}
      <tr><td>{{ j.nombre }}</td><td>{{ j.tier_actual }} {{ j.rank_actual }}</td><td>{{ j.elo_previo or '-' }}</td></tr>
      {% endfor %}
    </table>
    </div>
  </div>
  {% endif %}

  <div class="divisor"><span>◆</span></div>
  <div id="escudos" class="categoria con-arte" {% if fondo_secundario %}style="background-image:url('{{ fondo_secundario }}');"{% endif %}>
    <h2>Como conseguir un Escudo Azul</h2>
    <div class="info-grid">
      <div class="info-card"><div class="info-icono"><svg class="icon" viewBox="0 0 24 24"><path d="M12 2c1 3-2 4-2 7a4 4 0 1 0 8 0c0-2-1-3-1-5 2 1 3 4 3 7a6 6 0 1 1-12 0c0-4 2-6 4-9z"/></svg></div><div class="titulo-card">Pentakill / Cuadrakill</div></div>
      <div class="info-card"><div class="info-icono"><svg class="icon" viewBox="0 0 24 24"><path d="M4 4l16 16M20 4L4 20"/></svg></div><div class="titulo-card">22+ kills o 30+ asistencias</div></div>
      <div class="info-card"><div class="info-icono"><svg class="icon" viewBox="0 0 24 24"><path d="M3 17l6-6 4 4 8-8M15 7h6v6"/></svg></div><div class="titulo-card">KDA perfecto &gt; 20</div></div>
      <div class="info-card"><div class="info-icono"><svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l4 2"/></svg></div><div class="titulo-card">Victoria de 40+ minutos</div></div>
      <div class="info-card"><div class="info-icono"><svg class="icon" viewBox="0 0 24 24"><path d="M4 4v5h5M20 20v-5h-5"/><path d="M4.5 15a8 8 0 0 0 14.5 3.5M19.5 9A8 8 0 0 0 5 5.5"/></svg></div><div class="titulo-card">Racha de 6 victorias</div></div>
      <div class="info-card"><div class="info-icono"><svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M9 9h3a2 2 0 1 1 0 4H9m0 0h3a2 2 0 1 1 0 4H9M12 6v2M12 16v2"/></svg></div><div class="titulo-card">Comeback de 7000+ de oro</div></div>
      <div class="info-card"><div class="info-icono"><svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/></svg></div><div class="titulo-card">5 victorias con el mismo campeon</div></div>
      <div class="info-card"><div class="info-icono"><svg class="icon" viewBox="0 0 24 24"><path d="M12 2l2 2-8 8-2-2 8-8z"/><path d="M6 12l2 2-4 4-2-2 4-4z"/><path d="M14 4l6 6"/></svg></div><div class="titulo-card">Vencer a alguien con Blue Shell (se la robas)</div></div>
    </div>
  </div>
</div>
<footer>
  <div class="divisor"><span>◆</span></div>
  <div class="creditos {{ 'con-banner' if discord_banner else '' }}" {% if discord_banner %}style="background-image:url('{{ discord_banner }}');"{% endif %}>
    {% if discord_nombre %}
    <div class="creditos-servidor">
      {% if discord_icono %}<img src="{{ discord_icono }}" alt="">{% endif %}
      <span>Un proyecto de la comunidad de<br><span class="nombre">{{ discord_nombre }}</span></span>
    </div>
    {% endif %}
    {% if discord_invite %}
    <a class="btn-discord" href="{{ discord_invite }}" target="_blank" rel="noopener">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M20.317 4.369a19.79 19.79 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.058a.082.082 0 0 0 .031.056 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028 14.09 14.09 0 0 0 1.226-1.994.076.076 0 0 0-.041-.106 13.107 13.107 0 0 1-1.872-.892.077.077 0 0 1-.008-.128c.126-.094.252-.192.372-.291a.074.074 0 0 1 .077-.01c3.927 1.793 8.18 1.793 12.061 0a.073.073 0 0 1 .078.01c.12.099.246.198.373.292a.077.077 0 0 1-.006.127c-.598.35-1.22.645-1.873.893a.076.076 0 0 0-.04.106c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.029 19.84 19.84 0 0 0 6.002-3.03.077.077 0 0 0 .032-.055c.5-5.177-.838-9.674-3.548-13.662a.06.06 0 0 0-.031-.028zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.955 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.946 2.418-2.157 2.418z"/></svg>
      Unete a nuestro Discord
    </a>
    {% endif %}
    <div class="creditos-riot">Arte e iconos de campeones cortesia de Riot Games (Data Dragon / Community Dragon). SoloQ Challenge no esta afiliado a Riot Games.</div>
  </div>
  <div style="margin-top:18px;">Actualizado automaticamente - Pagina se refresca cada 60 segundos</div>
</footer>
<script>
function mostrarTab(id, btn) {
  document.querySelectorAll('.tab-panel').forEach(function(p) { p.classList.remove('activo'); });
  document.getElementById('tab-' + id).classList.add('activo');
  document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('activo'); });
  btn.classList.add('activo');
}
function filtrarJugadores(valor) {
  const q = valor.trim().toLowerCase();
  document.querySelectorAll('table.tabla-jugadores tr[data-nombre]').forEach(function(tr) {
    tr.style.display = tr.getAttribute('data-nombre').indexOf(q) !== -1 ? '' : 'none';
  });
}
const finTorneo = {{ fin_torneo_iso|tojson if fin_torneo_iso else 'null' }};
function actualizarCountdown() {
  if (!finTorneo) { return; }
  const ahora = new Date();
  const fin = new Date(finTorneo);
  let diff = fin - ahora;
  let texto;
  if (diff <= 0) { texto = 'Torneo finalizado'; }
  else {
    const d = Math.floor(diff / 86400000); diff -= d * 86400000;
    const h = Math.floor(diff / 3600000); diff -= h * 3600000;
    const m = Math.floor(diff / 60000); diff -= m * 60000;
    const s = Math.floor(diff / 1000);
    texto = d + 'd ' + h + 'h ' + m + 'm ' + s + 's para el cierre';
  }
  const el = document.getElementById('countdown');
  if (el) el.textContent = texto;
  const elNav = document.getElementById('countdown-nav');
  if (elNav) elNav.textContent = texto;
}
actualizarCountdown();
setInterval(actualizarCountdown, 1000);
</script>
</body>
</html>
"""


@app.route('/')
def home():
    db = cargar_db()
    high, low, pendientes, sin_voz = calcular_tabla(db)
    campeon_destacado = random.choice(CAMPEONES_POOL)
    campeon_secundario = random.choice(CAMPEONES_POOL)
    fin_torneo_iso = None
    if db.get('torneo_iniciado'):
        try:
            inicio = datetime.datetime.fromisoformat(db.get('inicio_torneo'))
            fin_torneo_iso = (inicio + datetime.timedelta(days=DURACION_TORNEO)).isoformat()
        except Exception:
            fin_torneo_iso = None
    particulas = [
        {'left': random.randint(0, 100), 'dur': round(random.uniform(9, 20), 1),
         'delay': round(random.uniform(0, 15), 1), 'size': random.randint(2, 4)}
        for _ in range(26)
    ]
    campeones_galeria = [
        {'nombre': c, 'icono': icono_campeon(c)}
        for c in random.sample(CAMPEONES_POOL, min(20, len(CAMPEONES_POOL)))
    ]
    total_castigos_pendientes = sum(j.get('castigos_pendientes', 0) for j in high + low)
    return render_template_string(PAGINA_HTML, high=high, low=low, pendientes=pendientes, sin_voz=sin_voz,
                                   total_castigos_pendientes=total_castigos_pendientes,
                                   duracion=DURACION_TORNEO, estado_torneo=calcular_estado_torneo(db),
                                   premio=PREMIO_GANADOR_USD,
                                   fondo_url=splash_campeon(campeon_destacado),
                                   fondo_secundario=splash_campeon(campeon_secundario),
                                   campeon_destacado=campeon_destacado,
                                   icono_destacado=icono_campeon(campeon_destacado),
                                   tier_colors=TIER_COLORES_WEB,
                                   emblema_rango=emblema_rango,
                                   campeones_galeria=campeones_galeria,
                                   fin_torneo_iso=fin_torneo_iso,
                                   particulas=particulas,
                                   discord_nombre=DISCORD_GUILD_NOMBRE,
                                   discord_invite=DISCORD_INVITE_URL,
                                   discord_icono=DISCORD_GUILD_ICON,
                                   discord_banner=DISCORD_GUILD_BANNER)

@app.route('/api/tabla')
def api_tabla():
    db = cargar_db()
    high, low, pendientes, sin_voz = calcular_tabla(db)
    return jsonify({'high': high, 'low': low, 'pendientes': pendientes, 'sin_voz': sin_voz,
                     'estado_torneo': calcular_estado_torneo(db)})


Thread(target=lambda: app.run(host='0.0.0.0', port=10000), daemon=True).start()


def _manejar_apagado(signum, frame):
    """Render envia SIGTERM al servicio en cada redeploy/reinicio. Antes de que el proceso
    muera, se intenta un ultimo volcado sincrono de la cache a Sheets para no perder los cambios
    que aun no habian llegado al proximo ciclo de sincronizar_sheets (maximo FLUSH_INTERVALO_SEG
    segundos de cambios en juego)."""
    print('Señal de apagado recibida, sincronizando cambios pendientes con Google Sheets...')
    try:
        flush_total_sincrono()
        print('Sincronizacion final completada.')
    except Exception as e:
        print(f'Error sincronizando antes de apagar: {e}')
    sys.exit(0)


signal.signal(signal.SIGTERM, _manejar_apagado)
signal.signal(signal.SIGINT, _manejar_apagado)

client.run(DISCORD_TOKEN)
