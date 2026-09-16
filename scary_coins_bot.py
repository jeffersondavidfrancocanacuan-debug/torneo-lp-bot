"""
Scary Coins Bot
----------------
Economia virtual del servidor: sueldo mensual, bono diario, logros (mas dificiles que los del
SoloQ Challenge) y apuestas en partidas de Flex, SoloQ y Personalizadas del propio Discord.
Los Scary Coins NO tienen valor monetario real por si mismos; se pueden canjear por premios (RP,
pase de batalla, etc.) mediante una solicitud que revisa el staff a mano, porque Riot no tiene una
API para entregar RP automaticamente.

Reglas clave pedidas por el dueno del servidor:
  - Nadie puede apostar en su propia partida, solo en las de los demas.
  - Los logros automaticos se calculan sobre partidas de FLEX (queue 440), separado del sistema de
    SoloQ Challenge (que ya vive en el otro bot y usa Solo/Duo, queue 420).
  - Economia "moderada": el sueldo + logros + apuestas dan un flujo constante, pero los premios
    grandes de la tienda cuestan semanas/meses de juego consistente (numeros ajustables abajo).

Comparte el Google Sheet y la Riot API key con los otros bots (GOOGLE_CREDENTIALS_JSON,
GOOGLE_SHEET_NAME, RIOT_API_KEY) pero usa sus propias hojas:
  - economia_saldos                  -> saldo de Scary Coins por jugador
  - economia_vinculos                -> Discord <-> cuenta de Riot (para detectar partidas y elo)
  - economia_logros                  -> logros ya otorgados (para no pagar el mismo dos veces)
  - economia_apuestas                -> apuestas activas e historicas
  - economia_canjes                  -> solicitudes de canje pendientes de entrega manual
  - apuestas_partida_flex            -> partidas de Flex abiertas a apuestas (las crea este bot)
  - apuestas_partida_personalizada   -> la mantiene personalizadas_bot.py, este bot solo la lee
"""

import discord
from discord import app_commands
from discord.ext import tasks
import requests
import json
import os
import time
import random
import datetime
from threading import Lock

HTTP_SESSION = requests.Session()

DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
RIOT_API_KEY = os.environ.get('RIOT_API_KEY')
DISCORD_GUILD_ID = os.environ.get('DISCORD_GUILD_ID', '331997851355709451')
CANAL_STAFF_CANJES_ID = os.environ.get('CANAL_STAFF_CANJES_ID', '')

GOOGLE_SHEET_NAME = os.environ.get('GOOGLE_SHEET_NAME', 'SoloQ Challenge DB')
GOOGLE_CREDENTIALS_JSON = os.environ.get('GOOGLE_CREDENTIALS_JSON', '')

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

MONEDA_EMOJI = '\U0001FA99'
MONEDA_NOMBRE = 'Scary Coins'

PLATFORM_MAP = {
    'lan': 'la1', 'na': 'na1', 'las': 'la2', 'euw': 'euw1', 'eune': 'eun1',
    'br': 'br1', 'tr': 'tr1', 'ru': 'ru', 'oce': 'oc1', 'jp': 'jp1', 'kr': 'kr'
}
REGION_MAP = {
    'lan': 'americas', 'na': 'americas', 'las': 'americas', 'euw': 'europe',
    'eune': 'europe', 'br': 'americas', 'tr': 'europe', 'ru': 'europe',
    'oce': 'americas', 'jp': 'asia', 'kr': 'asia'
}

TIER_VALOR = {
    'IRON': 0, 'BRONZE': 400, 'SILVER': 800, 'GOLD': 1200, 'PLATINUM': 1600,
    'EMERALD': 2000, 'DIAMOND': 2400, 'MASTER': 2800, 'GRANDMASTER': 3200, 'CHALLENGER': 3600,
}
RANK_VALOR = {'IV': 0, 'III': 100, 'II': 200, 'I': 300}

QUEUE_FLEX = 440  # Flex 5v5.
QUEUE_SOLO = 420  # Solo/Duo. Los logros siguen siendo solo de Flex, pero las apuestas en vivo
                   # (mismo comando /apostar_flex) ahora cubren las dos colas.
QUEUES_APOSTABLES = {QUEUE_FLEX: 'Flex', QUEUE_SOLO: 'SoloQ'}
RANKED_QUEUE_TYPE = {QUEUE_FLEX: 'RANKED_FLEX_SR', QUEUE_SOLO: 'RANKED_SOLO_5x5'}

# ---------------------------------------------------------------------------
# Numeros de la economia (ajustables)
# ---------------------------------------------------------------------------

SUELDO_MENSUAL = 500
BONUS_DIARIO_MIN, BONUS_DIARIO_MAX = 20, 40
BONUS_DIARIO_COOLDOWN_HORAS = 20
SALDO_INICIAL = 300  # Coins que se entregan una sola vez al vincular la cuenta por primera vez.

HOUSE_EDGE = 0.10          # 10% de recorte sobre el pago de apuestas, para sostener la economia
APUESTA_MINIMA = 10
APUESTA_MAX_PORCENTAJE_SALDO = 0.5
VENTANA_APUESTA_FLEX_MIN = 5      # minutos desde que se detecta la partida para poder apostar
EXPIRAR_PARTIDA_FLEX_HORAS = 3    # si no se resuelve en este tiempo, se cancela y se devuelve todo

ACHIEVEMENTS = {
    'pentakill': {'nombre': 'Pentakill', 'desc': 'Conseguir un Pentakill en una Flex.', 'coins': 150},
    'kda_perfecto': {'nombre': 'KDA Perfecto', 'desc': '10+ asesinatos sin morir en una Flex.', 'coins': 80},
    'carry_absoluto': {'nombre': 'Carry Absoluto', 'desc': 'Ganar una Flex con 15+ asesinatos.', 'coins': 100},
    'racha5': {'nombre': 'Racha de 5', 'desc': 'Ganar 5 Flex seguidas.', 'coins': 200},
}

TIENDA = [
    {'id': 'emblema_sorpresa', 'nombre': 'Emblema/skin shard sorpresa', 'costo': 1000},
    {'id': 'rp_150', 'nombre': '150 RP', 'costo': 3000},
    {'id': 'rp_350', 'nombre': '350 RP', 'costo': 6000},
    {'id': 'pase_batalla', 'nombre': 'Pase de batalla del evento actual', 'costo': 12000},
    {'id': 'rp_650', 'nombre': '650 RP', 'costo': 12000},
    {'id': 'rp_1350', 'nombre': '1350 RP', 'costo': 25000},
]

intents_ok = True

# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

_gs_client = None
_gs_spreadsheet = None
_cache_lock = Lock()


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
        ws = sheet.add_worksheet(title=nombre, rows=2000, cols=max(20, len(headers)))
        ws.update('A1', [headers], value_input_option='RAW')
    return ws


def _con_reintentos(func, intentos=3, espera_base=2):
    ultimo_error = None
    for intento in range(intentos):
        try:
            return func()
        except Exception as e:
            ultimo_error = e
            if intento < intentos - 1:
                time.sleep(espera_base * (intento + 1))
    raise ultimo_error


def _leer_tabla(nombre, headers):
    with _cache_lock:
        ws = _con_reintentos(lambda: _get_or_create_worksheet(nombre, headers))
        filas = _con_reintentos(lambda: ws.get_all_records(expected_headers=headers))
        return ws, filas


def _escribir_fila(ws, headers, fila_dict):
    valores = [str(fila_dict.get(h, '')) for h in headers]
    _con_reintentos(lambda: ws.append_row(valores, value_input_option='RAW'))


def _actualizar_fila(ws, headers, fila_index, fila_dict):
    valores = [str(fila_dict.get(h, '')) for h in headers]
    rango = f'A{fila_index + 2}'
    _con_reintentos(lambda: ws.update(rango, [valores], value_input_option='RAW'))


SALDOS_HEADERS = ['discord_id', 'nombre', 'saldo', 'mes_ultimo_sueldo', 'ultimo_bonus_diario']
VINCULOS_HEADERS = ['discord_id', 'nombre_discord', 'puuid', 'riot_id', 'region']
LOGROS_HEADERS = ['discord_id', 'logro_id', 'match_id', 'fecha', 'coins']
APUESTAS_HEADERS = ['id', 'tipo', 'referencia', 'discord_id_apostador', 'nombre_apostador',
                     'objetivo', 'monto', 'cuota', 'estado', 'fecha']
CANJES_HEADERS = ['discord_id', 'nombre', 'item', 'costo', 'estado', 'fecha']
PARTIDA_FLEX_HEADERS = ['match_id', 'jugadores_ids', 'equipo1_ids', 'equipo1_nombres', 'equipo2_ids',
                         'equipo2_nombres', 'elo1', 'elo2', 'prob1', 'prob2', 'region', 'estado',
                         'ganador', 'hora_deteccion', 'cierre_apuestas']
APUESTA_PERSONALIZADA_HEADERS = ['guild_id', 'equipo_a_ids', 'equipo_a_nombres', 'equipo_b_ids',
                                  'equipo_b_nombres', 'elo_a', 'elo_b', 'prob_a', 'prob_b', 'estado',
                                  'ganador', 'hora_armado']


# ---------------------------------------------------------------------------
# Saldo / economia
# ---------------------------------------------------------------------------

def _obtener_saldo_fila(discord_id):
    ws, filas = _leer_tabla('economia_saldos', SALDOS_HEADERS)
    idx = next((i for i, f in enumerate(filas) if f['discord_id'] == discord_id), None)
    return ws, filas, idx


def _otorgar_coins(discord_id, nombre, cantidad):
    """Suma (o resta, si cantidad es negativa) coins al saldo de un jugador. Devuelve el saldo nuevo."""
    ws, filas, idx = _obtener_saldo_fila(discord_id)
    if idx is None:
        saldo_actual = 0
        fila = {'discord_id': discord_id, 'nombre': nombre, 'saldo': 0,
                'mes_ultimo_sueldo': '', 'ultimo_bonus_diario': ''}
    else:
        fila = dict(filas[idx])
        try:
            saldo_actual = int(fila.get('saldo', 0) or 0)
        except (TypeError, ValueError):
            saldo_actual = 0
    nuevo_saldo = max(0, saldo_actual + cantidad)
    fila['saldo'] = nuevo_saldo
    fila['nombre'] = nombre
    if idx is None:
        _escribir_fila(ws, SALDOS_HEADERS, fila)
    else:
        _actualizar_fila(ws, SALDOS_HEADERS, idx, fila)
    return nuevo_saldo


def _prob_victoria(elo_propio, elo_rival):
    return 1 / (1 + 10 ** ((elo_rival - elo_propio) / 400))


def _cuota(prob):
    """Cuota decimal (cuanto se multiplica lo apostado si gana), con el house edge aplicado."""
    prob = max(prob, 0.03)
    return round((1 / prob) * (1 - HOUSE_EDGE), 2)


# ---------------------------------------------------------------------------
# Riot API
# ---------------------------------------------------------------------------

def _riot_get(url, timeout=8, intentos=3):
    headers = {'X-Riot-Token': RIOT_API_KEY}
    r = None
    for intento in range(intentos):
        r = HTTP_SESSION.get(url, headers=headers, timeout=timeout)
        if r.status_code == 429 and intento < intentos - 1:
            espera = r.headers.get('Retry-After')
            try:
                espera = float(espera)
            except (TypeError, ValueError):
                espera = 1.5
            time.sleep(min(espera, 5) + 0.2)
            continue
        return r
    return r


def obtener_puuid(riot_id, region):
    if '#' not in riot_id:
        return None, None
    region_base = REGION_MAP.get(region.lower())
    if not region_base:
        return None, None
    game_name, tag_line = riot_id.split('#', 1)
    url = f'https://{region_base}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}'
    r = _riot_get(url)
    if r is None or r.status_code != 200:
        return None, None
    cuenta = r.json()
    return cuenta['puuid'], f"{cuenta['gameName']}#{cuenta['tagLine']}"


def obtener_elo_ranked(puuid, region, ranked_queue_type):
    """Elo numerico en la cola ranked indicada (RANKED_FLEX_SR o RANKED_SOLO_5x5). 0 si no tiene rango."""
    plataforma = PLATFORM_MAP.get(region.lower())
    if not plataforma:
        return 0
    url = f'https://{plataforma}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}'
    r = _riot_get(url)
    if r is None or r.status_code != 200:
        return 0
    for entry in r.json():
        if entry['queueType'] == ranked_queue_type:
            return TIER_VALOR.get(entry['tier'], 0) + RANK_VALOR.get(entry['rank'], 0) + entry['leaguePoints']
    return 0


def obtener_elo_flex(puuid, region):
    """Elo numerico en Flex (RANKED_FLEX_SR) para un puuid. 0 si no tiene rango."""
    return obtener_elo_ranked(puuid, region, 'RANKED_FLEX_SR')


def obtener_partida_en_vivo(puuid, region):
    plataforma = PLATFORM_MAP.get(region.lower())
    if not plataforma:
        return None
    url = f'https://{plataforma}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}'
    r = _riot_get(url)
    if r is None or r.status_code != 200:
        return None
    return r.json()


def obtener_ultimos_match_ids(puuid, region, count=1, queue=QUEUE_FLEX):
    region_base = REGION_MAP.get(region.lower())
    if not region_base:
        return []
    url = (f'https://{region_base}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids'
           f'?start=0&count={count}')
    if queue is not None:
        url += f'&queue={queue}'
    r = _riot_get(url)
    if r is None or r.status_code != 200:
        return []
    return r.json()


def obtener_match(match_id, region):
    region_base = REGION_MAP.get(region.lower())
    if not region_base:
        return None
    url = f'https://{region_base}.api.riotgames.com/lol/match/v5/matches/{match_id}'
    r = _riot_get(url)
    if r is None or r.status_code != 200:
        return None
    return r.json()


# ---------------------------------------------------------------------------
# Vinculos Discord <-> Riot
# ---------------------------------------------------------------------------

def _obtener_vinculo(discord_id):
    _, filas = _leer_tabla('economia_vinculos', VINCULOS_HEADERS)
    return next((f for f in filas if f['discord_id'] == discord_id), None)


def _todos_los_vinculos():
    _, filas = _leer_tabla('economia_vinculos', VINCULOS_HEADERS)
    return filas


# ---------------------------------------------------------------------------
# Comandos: cuenta y saldo
# ---------------------------------------------------------------------------

@tree.command(name='vincular', description=f'Vincula tu cuenta de Riot para poder ganar {MONEDA_NOMBRE}')
@app_commands.describe(riot_id='Tu Riot ID, formato Nombre#TAG', region='Tu region (lan, na, euw, etc.)')
async def vincular(interaction: discord.Interaction, riot_id: str, region: str):
    await interaction.response.defer(ephemeral=True)
    if region.lower() not in PLATFORM_MAP:
        await interaction.followup.send('Region no reconocida. Usa algo como lan, na, euw, eune, br...',
                                         ephemeral=True)
        return
    puuid, nombre_completo = obtener_puuid(riot_id, region)
    if puuid is None:
        await interaction.followup.send('No pude encontrar esa cuenta de Riot. Revisa el Riot ID y la region.',
                                         ephemeral=True)
        return
    ws, filas = _leer_tabla('economia_vinculos', VINCULOS_HEADERS)
    discord_id = str(interaction.user.id)
    idx = next((i for i, f in enumerate(filas) if f['discord_id'] == discord_id), None)
    fila = {'discord_id': discord_id, 'nombre_discord': interaction.user.display_name,
            'puuid': puuid, 'riot_id': nombre_completo, 'region': region.lower()}
    if idx is None:
        _escribir_fila(ws, VINCULOS_HEADERS, fila)
    else:
        _actualizar_fila(ws, VINCULOS_HEADERS, idx, fila)

    # Saldo inicial de regalo, solo la primera vez que esta persona aparece en la economia
    # (si ya tenia saldo -aunque sea 0 por haber usado /sueldo o /bonus_diario antes- no se le vuelve a dar).
    _, _, idx_saldo = _obtener_saldo_fila(discord_id)
    mensaje_saldo = ''
    if idx_saldo is None:
        nuevo_saldo = _otorgar_coins(discord_id, interaction.user.display_name, SALDO_INICIAL)
        mensaje_saldo = f' Como regalo de bienvenida ya tenes **{nuevo_saldo} {MONEDA_EMOJI}** para apostar.'

    await interaction.followup.send(
        f'Cuenta vinculada: **{nombre_completo}**. Ya podes ganar logros automaticos en Flex y '
        f'apostar en las partidas de otros (Flex, SoloQ y Personalizadas).{mensaje_saldo}', ephemeral=True)


@tree.command(name='saldo', description=f'Ver cuantos {MONEDA_NOMBRE} tenes (o los de otro)')
@app_commands.describe(usuario='De quien queres ver el saldo (opcional)')
async def saldo(interaction: discord.Interaction, usuario: discord.Member = None):
    objetivo = usuario or interaction.user
    _, _, idx = _obtener_saldo_fila(str(objetivo.id))
    ws, filas = _leer_tabla('economia_saldos', SALDOS_HEADERS)
    fila = next((f for f in filas if f['discord_id'] == str(objetivo.id)), None)
    monto = fila['saldo'] if fila else 0
    await interaction.response.send_message(
        f'{objetivo.display_name} tiene **{monto} {MONEDA_EMOJI} {MONEDA_NOMBRE}**.')


@tree.command(name='sueldo', description='Reclama tu sueldo mensual de Scary Coins')
async def sueldo(interaction: discord.Interaction):
    discord_id = str(interaction.user.id)
    ws, filas, idx = _obtener_saldo_fila(discord_id)
    mes_actual = datetime.datetime.utcnow().strftime('%Y-%m')
    mes_reclamado = filas[idx]['mes_ultimo_sueldo'] if idx is not None else ''
    if mes_reclamado == mes_actual:
        await interaction.response.send_message('Ya reclamaste tu sueldo este mes. Volve el mes que viene.',
                                                  ephemeral=True)
        return
    nuevo_saldo = _otorgar_coins(discord_id, interaction.user.display_name, SUELDO_MENSUAL)
    ws2, filas2, idx2 = _obtener_saldo_fila(discord_id)
    fila = dict(filas2[idx2])
    fila['mes_ultimo_sueldo'] = mes_actual
    _actualizar_fila(ws2, SALDOS_HEADERS, idx2, fila)
    await interaction.response.send_message(
        f'Sueldo cobrado: +{SUELDO_MENSUAL} {MONEDA_EMOJI}. Saldo actual: {nuevo_saldo} {MONEDA_EMOJI}.')


@tree.command(name='bonus_diario', description=f'Reclama tu bono diario de {MONEDA_NOMBRE}')
async def bonus_diario(interaction: discord.Interaction):
    discord_id = str(interaction.user.id)
    ws, filas, idx = _obtener_saldo_fila(discord_id)
    ahora = datetime.datetime.utcnow()
    if idx is not None and filas[idx].get('ultimo_bonus_diario'):
        try:
            ultimo = datetime.datetime.fromisoformat(filas[idx]['ultimo_bonus_diario'])
            horas_pasadas = (ahora - ultimo).total_seconds() / 3600
            if horas_pasadas < BONUS_DIARIO_COOLDOWN_HORAS:
                faltan = round(BONUS_DIARIO_COOLDOWN_HORAS - horas_pasadas, 1)
                await interaction.response.send_message(
                    f'Todavia no. Te faltan {faltan} horas para el proximo bono.', ephemeral=True)
                return
        except ValueError:
            pass
    monto = random.randint(BONUS_DIARIO_MIN, BONUS_DIARIO_MAX)
    nuevo_saldo = _otorgar_coins(discord_id, interaction.user.display_name, monto)
    ws2, filas2, idx2 = _obtener_saldo_fila(discord_id)
    fila = dict(filas2[idx2])
    fila['ultimo_bonus_diario'] = ahora.isoformat()
    _actualizar_fila(ws2, SALDOS_HEADERS, idx2, fila)
    await interaction.response.send_message(
        f'Bono diario: +{monto} {MONEDA_EMOJI}. Saldo actual: {nuevo_saldo} {MONEDA_EMOJI}.')


@tree.command(name='ranking_coins', description=f'Top de {MONEDA_NOMBRE} del servidor')
async def ranking_coins(interaction: discord.Interaction):
    _, filas = _leer_tabla('economia_saldos', SALDOS_HEADERS)
    if not filas:
        await interaction.response.send_message('Todavia nadie tiene saldo.', ephemeral=True)
        return
    filas.sort(key=lambda f: int(f['saldo'] or 0), reverse=True)
    lineas = [f"{i + 1}. **{f['nombre']}** - {f['saldo']} {MONEDA_EMOJI}" for i, f in enumerate(filas[:15])]
    embed = discord.Embed(title=f'Ranking de {MONEDA_NOMBRE}', description='\n'.join(lineas), color=0xF1C40F)
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Comandos: logros
# ---------------------------------------------------------------------------

@tree.command(name='logros', description='Ver el catalogo de logros y sus recompensas')
async def logros(interaction: discord.Interaction):
    lineas = [f"**{a['nombre']}** ({a['coins']} {MONEDA_EMOJI}) - {a['desc']}" for a in ACHIEVEMENTS.values()]
    embed = discord.Embed(title='Logros de Scary Coins', description='\n\n'.join(lineas), color=0x9B59B6)
    embed.set_footer(text='Se revisan automaticamente cada rato para cuentas vinculadas con /vincular.')
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name='mis_logros', description='Ver los logros que ya conseguiste (o los de otro)')
@app_commands.describe(usuario='De quien queres ver los logros (opcional)')
async def mis_logros(interaction: discord.Interaction, usuario: discord.Member = None):
    objetivo = usuario or interaction.user
    _, filas = _leer_tabla('economia_logros', LOGROS_HEADERS)
    propios = [f for f in filas if f['discord_id'] == str(objetivo.id)]
    if not propios:
        await interaction.response.send_message(f'{objetivo.display_name} todavia no tiene logros.',
                                                  ephemeral=True)
        return
    conteo = {}
    total = 0
    for f in propios:
        base_id = f['logro_id'].split(':')[0]
        conteo[base_id] = conteo.get(base_id, 0) + 1
        total += int(f['coins'] or 0)
    lineas = []
    for logro_id, cant in conteo.items():
        nombre = ACHIEVEMENTS.get(logro_id, {}).get('nombre', logro_id)
        lineas.append(f'**{nombre}** x{cant}')
    embed = discord.Embed(title=f'Logros de {objetivo.display_name}', description='\n'.join(lineas),
                           color=0x9B59B6)
    embed.set_footer(text=f'Total ganado por logros: {total} {MONEDA_NOMBRE}')
    await interaction.response.send_message(embed=embed)


def _ya_otorgado(discord_id, logro_id, match_id):
    _, filas = _leer_tabla('economia_logros', LOGROS_HEADERS)
    return any(f['discord_id'] == discord_id and f['logro_id'] == logro_id and f['match_id'] == match_id
               for f in filas)


def _registrar_logro(discord_id, logro_id, match_id, coins):
    ws, _ = _leer_tabla('economia_logros', LOGROS_HEADERS)
    _escribir_fila(ws, LOGROS_HEADERS, {
        'discord_id': discord_id, 'logro_id': logro_id, 'match_id': match_id,
        'fecha': datetime.datetime.utcnow().isoformat(), 'coins': coins,
    })


def _revisar_logros_de_cuenta(discord_id, nombre, puuid, region, profundo=False):
    """Revisa la ultima Flex (o las ultimas 5 si profundo=True) de una cuenta vinculada y otorga
    los logros nuevos que encuentre. Devuelve una lista de nombres de logros otorgados."""
    otorgados = []
    count = 5 if profundo else 1
    match_ids = obtener_ultimos_match_ids(puuid, region, count=count, queue=QUEUE_FLEX)
    if not match_ids:
        return otorgados

    resultados_recientes = []  # (match_id, gano)
    for match_id in match_ids:
        match = obtener_match(match_id, region)
        if not match:
            continue
        participantes = match.get('info', {}).get('participants', [])
        p = next((x for x in participantes if x.get('puuid') == puuid), None)
        if not p:
            continue
        resultados_recientes.append((match_id, p.get('win', False)))

        if p.get('pentaKills', 0) >= 1 and not _ya_otorgado(discord_id, 'pentakill', match_id):
            coins = ACHIEVEMENTS['pentakill']['coins']
            _registrar_logro(discord_id, 'pentakill', match_id, coins)
            _otorgar_coins(discord_id, nombre, coins)
            otorgados.append(ACHIEVEMENTS['pentakill']['nombre'])

        if p.get('kills', 0) >= 10 and p.get('deaths', 0) == 0 and not _ya_otorgado(discord_id, 'kda_perfecto', match_id):
            coins = ACHIEVEMENTS['kda_perfecto']['coins']
            _registrar_logro(discord_id, 'kda_perfecto', match_id, coins)
            _otorgar_coins(discord_id, nombre, coins)
            otorgados.append(ACHIEVEMENTS['kda_perfecto']['nombre'])

        if p.get('win') and p.get('kills', 0) >= 15 and not _ya_otorgado(discord_id, 'carry_absoluto', match_id):
            coins = ACHIEVEMENTS['carry_absoluto']['coins']
            _registrar_logro(discord_id, 'carry_absoluto', match_id, coins)
            _otorgar_coins(discord_id, nombre, coins)
            otorgados.append(ACHIEVEMENTS['carry_absoluto']['nombre'])

    if profundo and len(resultados_recientes) >= 5:
        ultimas_5 = resultados_recientes[:5]
        if all(gano for _, gano in ultimas_5):
            match_racha = ultimas_5[0][0]
            if not _ya_otorgado(discord_id, 'racha5', match_racha):
                coins = ACHIEVEMENTS['racha5']['coins']
                _registrar_logro(discord_id, 'racha5', match_racha, coins)
                _otorgar_coins(discord_id, nombre, coins)
                otorgados.append(ACHIEVEMENTS['racha5']['nombre'])

    return otorgados


@tree.command(name='revisar_logros', description='Revisa tus ultimas Flex a fondo por si hay logros nuevos')
async def revisar_logros(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    discord_id = str(interaction.user.id)
    vinculo = _obtener_vinculo(discord_id)
    if vinculo is None:
        await interaction.followup.send('Primero vinculate con /vincular.', ephemeral=True)
        return
    otorgados = _revisar_logros_de_cuenta(discord_id, interaction.user.display_name,
                                           vinculo['puuid'], vinculo['region'], profundo=True)
    if otorgados:
        await interaction.followup.send('Logros nuevos: ' + ', '.join(otorgados) + '. Revisa /saldo.',
                                         ephemeral=True)
    else:
        await interaction.followup.send('No encontre logros nuevos en tus ultimas Flex.', ephemeral=True)


@tree.command(name='otorgar_logro', description='(Staff) Otorga coins manualmente por un logro o motivo especial')
@app_commands.describe(usuario='A quien premiar', cantidad='Cuantos Scary Coins', motivo='Por que se le otorgan')
async def otorgar_logro(interaction: discord.Interaction, usuario: discord.Member, cantidad: int, motivo: str):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message('Este comando es solo para staff.', ephemeral=True)
        return
    nuevo_saldo = _otorgar_coins(str(usuario.id), usuario.display_name, cantidad)
    _registrar_logro(str(usuario.id), f'manual:{motivo[:40]}', '', cantidad)
    await interaction.response.send_message(
        f'{usuario.display_name} recibio {cantidad} {MONEDA_EMOJI} por: {motivo}. '
        f'Saldo actual: {nuevo_saldo} {MONEDA_EMOJI}.')


# ---------------------------------------------------------------------------
# Comandos: apuestas en Flex
# ---------------------------------------------------------------------------

@tree.command(name='partida_en_vivo', description='Busca la Flex o SoloQ en vivo de alguien vinculado y la abre a apuestas')
@app_commands.describe(usuario='El jugador vinculado que esta jugando Flex o SoloQ ahora')
async def partida_en_vivo(interaction: discord.Interaction, usuario: discord.Member):
    await interaction.response.defer()
    vinculo = _obtener_vinculo(str(usuario.id))
    if vinculo is None:
        await interaction.followup.send(f'{usuario.display_name} no tiene cuenta vinculada (/vincular).')
        return
    partida = obtener_partida_en_vivo(vinculo['puuid'], vinculo['region'])
    if partida is None:
        await interaction.followup.send(f'{usuario.display_name} no esta en ninguna partida ahora mismo.')
        return
    queue_id = partida.get('gameQueueConfigId')
    nombre_queue = QUEUES_APOSTABLES.get(queue_id)
    if nombre_queue is None:
        await interaction.followup.send(f'{usuario.display_name} esta jugando, pero no es Flex ni SoloQ '
                                         '(este sistema de apuestas solo cubre esas dos colas).')
        return
    ranked_queue_type = RANKED_QUEUE_TYPE[queue_id]

    match_id = f"{PLATFORM_MAP[vinculo['region']].upper()}_{partida['gameId']}"
    participantes = partida.get('participants', [])
    vinculos = {v['puuid']: v for v in _todos_los_vinculos() if v.get('puuid')}

    equipo1_ids, equipo1_nombres, equipo1_elos = [], [], []
    equipo2_ids, equipo2_nombres, equipo2_elos = [], [], []
    jugadores_ids = []
    for p in participantes:
        puuid_p = p.get('puuid', '')
        v = vinculos.get(puuid_p)
        nombre_p = v['riot_id'] if v else p.get('riotId', p.get('summonerName', 'Jugador'))
        if v:
            jugadores_ids.append(v['discord_id'])
            elo_p = obtener_elo_ranked(puuid_p, v['region'], ranked_queue_type)
        else:
            elo_p = None
        if p.get('teamId') == 100:
            equipo1_nombres.append(nombre_p)
            if v:
                equipo1_ids.append(v['discord_id'])
            if elo_p is not None:
                equipo1_elos.append(elo_p)
        else:
            equipo2_nombres.append(nombre_p)
            if v:
                equipo2_ids.append(v['discord_id'])
            if elo_p is not None:
                equipo2_elos.append(elo_p)

    elo1 = round(sum(equipo1_elos) / len(equipo1_elos)) * 5 if equipo1_elos else 1200 * 5
    elo2 = round(sum(equipo2_elos) / len(equipo2_elos)) * 5 if equipo2_elos else 1200 * 5
    prob1 = _prob_victoria(elo1, elo2)

    ahora = datetime.datetime.utcnow()
    cierre = ahora + datetime.timedelta(minutes=VENTANA_APUESTA_FLEX_MIN)

    ws, filas = _leer_tabla('apuestas_partida_flex', PARTIDA_FLEX_HEADERS)
    idx = next((i for i, f in enumerate(filas) if f['match_id'] == match_id), None)
    fila = {
        'match_id': match_id, 'jugadores_ids': ','.join(jugadores_ids),
        'equipo1_ids': ','.join(equipo1_ids), 'equipo1_nombres': ', '.join(equipo1_nombres),
        'equipo2_ids': ','.join(equipo2_ids), 'equipo2_nombres': ', '.join(equipo2_nombres),
        'elo1': elo1, 'elo2': elo2, 'prob1': round(prob1, 4), 'prob2': round(1 - prob1, 4),
        'region': vinculo['region'], 'estado': 'abierta', 'ganador': '',
        'hora_deteccion': ahora.isoformat(), 'cierre_apuestas': cierre.isoformat(),
    }
    if idx is None:
        _escribir_fila(ws, PARTIDA_FLEX_HEADERS, fila)
    else:
        fila_previa = filas[idx]
        if fila_previa['estado'] not in ('resuelta', 'expirada'):
            fila = fila_previa  # ya estaba trackeada y sigue abierta/cerrada, no la piso
        _actualizar_fila(ws, PARTIDA_FLEX_HEADERS, idx, fila)

    embed = discord.Embed(title=f'{nombre_queue} en vivo detectada', color=0x3498DB,
                           description=f'Apuestas abiertas por {VENTANA_APUESTA_FLEX_MIN} minutos desde la '
                                       f'primera deteccion. ID: `{match_id}`')
    embed.add_field(name=f"Equipo 1 (cuota {_cuota(prob1)}x)", value='\n'.join(equipo1_nombres) or '-', inline=True)
    embed.add_field(name=f"Equipo 2 (cuota {_cuota(1 - prob1)}x)", value='\n'.join(equipo2_nombres) or '-', inline=True)
    embed.set_footer(text='Usa /apostar_flex para apostar (no podes apostar en tu propia partida).')
    await interaction.followup.send(embed=embed)


@tree.command(name='apostar_flex', description='Apuesta en una Flex o SoloQ en vivo que ya este trackeada')
@app_commands.describe(match_id='El ID de la partida (te lo muestra /partida_en_vivo)',
                        equipo='Equipo 1 o 2', monto=f'Cuantos {MONEDA_NOMBRE} apostar')
@app_commands.choices(equipo=[app_commands.Choice(name='Equipo 1', value='1'),
                               app_commands.Choice(name='Equipo 2', value='2')])
async def apostar_flex(interaction: discord.Interaction, match_id: str, equipo: app_commands.Choice[str], monto: int):
    discord_id = str(interaction.user.id)
    _, filas = _leer_tabla('apuestas_partida_flex', PARTIDA_FLEX_HEADERS)
    partida = next((f for f in filas if f['match_id'] == match_id), None)
    if partida is None:
        await interaction.response.send_message('No encuentro esa partida. Revisa el ID.', ephemeral=True)
        return
    if partida['estado'] != 'abierta':
        await interaction.response.send_message('Esa partida ya no acepta apuestas.', ephemeral=True)
        return
    try:
        cierre = datetime.datetime.fromisoformat(partida['cierre_apuestas'])
        if datetime.datetime.utcnow() > cierre:
            await interaction.response.send_message('Se cerro la ventana de apuestas para esta partida.',
                                                      ephemeral=True)
            return
    except ValueError:
        pass
    if discord_id in partida['jugadores_ids'].split(','):
        await interaction.response.send_message('No podes apostar en tu propia partida.', ephemeral=True)
        return
    if monto < APUESTA_MINIMA:
        await interaction.response.send_message(f'La apuesta minima es {APUESTA_MINIMA} {MONEDA_EMOJI}.',
                                                  ephemeral=True)
        return

    _, _, idx_saldo = _obtener_saldo_fila(discord_id)
    ws_s, filas_s = _leer_tabla('economia_saldos', SALDOS_HEADERS)
    saldo_actual = int(filas_s[idx_saldo]['saldo']) if idx_saldo is not None else 0
    if monto > saldo_actual:
        await interaction.response.send_message('No tenes suficiente saldo.', ephemeral=True)
        return
    if monto > saldo_actual * APUESTA_MAX_PORCENTAJE_SALDO:
        await interaction.response.send_message(
            f'No podes apostar mas del {int(APUESTA_MAX_PORCENTAJE_SALDO * 100)}% de tu saldo de una vez.',
            ephemeral=True)
        return

    prob = float(partida['prob1']) if equipo.value == '1' else float(partida['prob2'])
    cuota = _cuota(prob)
    _otorgar_coins(discord_id, interaction.user.display_name, -monto)

    ws_a, _ = _leer_tabla('economia_apuestas', APUESTAS_HEADERS)
    _escribir_fila(ws_a, APUESTAS_HEADERS, {
        'id': f'{match_id}:{discord_id}:{int(time.time())}', 'tipo': 'flex', 'referencia': match_id,
        'discord_id_apostador': discord_id, 'nombre_apostador': interaction.user.display_name,
        'objetivo': equipo.value, 'monto': monto, 'cuota': cuota, 'estado': 'pendiente',
        'fecha': datetime.datetime.utcnow().isoformat(),
    })
    await interaction.response.send_message(
        f'Apostaste {monto} {MONEDA_EMOJI} al Equipo {equipo.value} (cuota {cuota}x). '
        f'Si gana, cobras {round(monto * cuota)} {MONEDA_EMOJI}.')


# ---------------------------------------------------------------------------
# Comandos: apuestas en Personalizadas
# ---------------------------------------------------------------------------

@tree.command(name='apostar_personalizada', description='Apuesta en la personalizada actual del servidor')
@app_commands.describe(equipo='Equipo A o B', monto=f'Cuantos {MONEDA_NOMBRE} apostar')
@app_commands.choices(equipo=[app_commands.Choice(name='Equipo A', value='A'),
                               app_commands.Choice(name='Equipo B', value='B')])
async def apostar_personalizada(interaction: discord.Interaction, equipo: app_commands.Choice[str], monto: int):
    discord_id = str(interaction.user.id)
    guild_id = str(interaction.guild_id)
    _, filas = _leer_tabla('apuestas_partida_personalizada', APUESTA_PERSONALIZADA_HEADERS)
    partida = next((f for f in filas if f['guild_id'] == guild_id), None)
    if partida is None or partida['estado'] != 'abierta':
        await interaction.response.send_message('No hay ninguna personalizada abierta a apuestas ahora mismo.',
                                                  ephemeral=True)
        return
    jugadores = partida['equipo_a_ids'].split(',') + partida['equipo_b_ids'].split(',')
    if discord_id in jugadores:
        await interaction.response.send_message('No podes apostar en tu propia partida.', ephemeral=True)
        return
    if monto < APUESTA_MINIMA:
        await interaction.response.send_message(f'La apuesta minima es {APUESTA_MINIMA} {MONEDA_EMOJI}.',
                                                  ephemeral=True)
        return

    ws_s, filas_s = _leer_tabla('economia_saldos', SALDOS_HEADERS)
    fila_s = next((f for f in filas_s if f['discord_id'] == discord_id), None)
    saldo_actual = int(fila_s['saldo']) if fila_s else 0
    if monto > saldo_actual:
        await interaction.response.send_message('No tenes suficiente saldo.', ephemeral=True)
        return
    if monto > saldo_actual * APUESTA_MAX_PORCENTAJE_SALDO:
        await interaction.response.send_message(
            f'No podes apostar mas del {int(APUESTA_MAX_PORCENTAJE_SALDO * 100)}% de tu saldo de una vez.',
            ephemeral=True)
        return

    prob = float(partida['prob_a']) if equipo.value == 'A' else float(partida['prob_b'])
    cuota = _cuota(prob)
    _otorgar_coins(discord_id, interaction.user.display_name, -monto)

    ws_a, _ = _leer_tabla('economia_apuestas', APUESTAS_HEADERS)
    _escribir_fila(ws_a, APUESTAS_HEADERS, {
        'id': f'{guild_id}:{discord_id}:{int(time.time())}', 'tipo': 'personalizada', 'referencia': guild_id,
        'discord_id_apostador': discord_id, 'nombre_apostador': interaction.user.display_name,
        'objetivo': equipo.value, 'monto': monto, 'cuota': cuota, 'estado': 'pendiente',
        'fecha': datetime.datetime.utcnow().isoformat(),
    })
    await interaction.response.send_message(
        f'Apostaste {monto} {MONEDA_EMOJI} al Equipo {equipo.value} (cuota {cuota}x). '
        f'Si gana, cobras {round(monto * cuota)} {MONEDA_EMOJI}.')


@tree.command(name='mis_apuestas', description='Ver tus apuestas pendientes')
async def mis_apuestas(interaction: discord.Interaction):
    discord_id = str(interaction.user.id)
    _, filas = _leer_tabla('economia_apuestas', APUESTAS_HEADERS)
    pendientes = [f for f in filas if f['discord_id_apostador'] == discord_id and f['estado'] == 'pendiente']
    if not pendientes:
        await interaction.response.send_message('No tenes apuestas pendientes.', ephemeral=True)
        return
    lineas = [f"**{f['tipo']}** - {f['monto']} {MONEDA_EMOJI} a Equipo {f['objetivo']} (cuota {f['cuota']}x)"
              for f in pendientes]
    embed = discord.Embed(title='Tus apuestas pendientes', description='\n'.join(lineas), color=0x2ECC71)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Tienda / canje
# ---------------------------------------------------------------------------

@tree.command(name='tienda', description=f'Ver el catalogo de canje de {MONEDA_NOMBRE}')
async def tienda(interaction: discord.Interaction):
    lineas = [f"**{item['nombre']}** - {item['costo']} {MONEDA_EMOJI}" for item in TIENDA]
    embed = discord.Embed(title='Tienda de Scary Coins', description='\n'.join(lineas), color=0xE91E63)
    embed.set_footer(text='Los premios en RP/pase se entregan a mano; usa /canjear para pedirlos.')
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name='canjear', description='Canjea Scary Coins por un premio de la tienda')
@app_commands.describe(item='Que queres canjear')
@app_commands.choices(item=[app_commands.Choice(name=f"{i['nombre']} ({i['costo']})", value=i['id']) for i in TIENDA])
async def canjear(interaction: discord.Interaction, item: app_commands.Choice[str]):
    catalogo = next(i for i in TIENDA if i['id'] == item.value)
    discord_id = str(interaction.user.id)
    ws_s, filas_s = _leer_tabla('economia_saldos', SALDOS_HEADERS)
    fila_s = next((f for f in filas_s if f['discord_id'] == discord_id), None)
    saldo_actual = int(fila_s['saldo']) if fila_s else 0
    if saldo_actual < catalogo['costo']:
        faltan = catalogo['costo'] - saldo_actual
        await interaction.response.send_message(
            f'Te faltan {faltan} {MONEDA_EMOJI} para canjear "{catalogo["nombre"]}".', ephemeral=True)
        return

    _otorgar_coins(discord_id, interaction.user.display_name, -catalogo['costo'])
    ws_c, _ = _leer_tabla('economia_canjes', CANJES_HEADERS)
    _escribir_fila(ws_c, CANJES_HEADERS, {
        'discord_id': discord_id, 'nombre': interaction.user.display_name, 'item': catalogo['nombre'],
        'costo': catalogo['costo'], 'estado': 'pendiente', 'fecha': datetime.datetime.utcnow().isoformat(),
    })
    await interaction.response.send_message(
        f'Canje solicitado: **{catalogo["nombre"]}**. El staff te va a contactar para entregartelo.')

    if CANAL_STAFF_CANJES_ID:
        try:
            canal = client.get_channel(int(CANAL_STAFF_CANJES_ID))
            if canal:
                await canal.send(
                    f'\U0001F514 Nuevo canje: **{interaction.user.mention}** pidio '
                    f'**{catalogo["nombre"]}** por {catalogo["costo"]} {MONEDA_EMOJI}.')
        except Exception as e:
            print(f'[scary_coins] No pude avisar en el canal de staff: {e}')


# ---------------------------------------------------------------------------
# Resolucion automatica de apuestas (loop de fondo)
# ---------------------------------------------------------------------------

def _pagar_apuestas_pendientes(tipo, referencia, ganador):
    ws_a, filas_a = _leer_tabla('economia_apuestas', APUESTAS_HEADERS)
    for i, f in enumerate(filas_a):
        if f['tipo'] != tipo or f['referencia'] != referencia or f['estado'] != 'pendiente':
            continue
        gano = (f['objetivo'] == ganador)
        nuevo_estado = 'ganada' if gano else 'perdida'
        if gano:
            premio = round(int(f['monto']) * float(f['cuota']))
            _otorgar_coins(f['discord_id_apostador'], f['nombre_apostador'], premio)
        fila = dict(f)
        fila['estado'] = nuevo_estado
        _actualizar_fila(ws_a, APUESTAS_HEADERS, i, fila)


def _reembolsar_apuestas_pendientes(tipo, referencia):
    ws_a, filas_a = _leer_tabla('economia_apuestas', APUESTAS_HEADERS)
    for i, f in enumerate(filas_a):
        if f['tipo'] != tipo or f['referencia'] != referencia or f['estado'] != 'pendiente':
            continue
        _otorgar_coins(f['discord_id_apostador'], f['nombre_apostador'], int(f['monto']))
        fila = dict(f)
        fila['estado'] = 'reembolsada'
        _actualizar_fila(ws_a, APUESTAS_HEADERS, i, fila)


@tasks.loop(minutes=3)
async def resolver_apuestas_loop():
    try:
        # --- Personalizadas: leer el estado que mantiene personalizadas_bot.py ---
        _, filas_p = _leer_tabla('apuestas_partida_personalizada', APUESTA_PERSONALIZADA_HEADERS)
        for f in filas_p:
            if f['estado'] == 'resuelta' and f['ganador']:
                _pagar_apuestas_pendientes('personalizada', f['guild_id'], f['ganador'])

        # --- Flex: cerrar ventana de apuestas vencidas y buscar partidas terminadas ---
        ws_f, filas_f = _leer_tabla('apuestas_partida_flex', PARTIDA_FLEX_HEADERS)
        ahora = datetime.datetime.utcnow()
        for i, f in enumerate(filas_f):
            if f['estado'] not in ('abierta', 'cerrada'):
                continue
            try:
                detectada = datetime.datetime.fromisoformat(f['hora_deteccion'])
            except ValueError:
                continue
            if f['estado'] == 'abierta':
                try:
                    cierre = datetime.datetime.fromisoformat(f['cierre_apuestas'])
                    if ahora > cierre:
                        fila = dict(f)
                        fila['estado'] = 'cerrada'
                        _actualizar_fila(ws_f, PARTIDA_FLEX_HEADERS, i, fila)
                except ValueError:
                    pass

            if (ahora - detectada).total_seconds() > EXPIRAR_PARTIDA_FLEX_HORAS * 3600:
                _reembolsar_apuestas_pendientes('flex', f['match_id'])
                fila = dict(f)
                fila['estado'] = 'expirada'
                _actualizar_fila(ws_f, PARTIDA_FLEX_HEADERS, i, fila)
                continue

            # revisar si ya termino, usando el primer jugador vinculado que tengamos de esa partida
            jugadores_ids = [j for j in f['jugadores_ids'].split(',') if j]
            if not jugadores_ids:
                continue
            vinculo = _obtener_vinculo(jugadores_ids[0])
            if not vinculo:
                continue
            ultimos = obtener_ultimos_match_ids(vinculo['puuid'], vinculo['region'], count=1, queue=None)
            if not ultimos or ultimos[0] != f['match_id']:
                continue  # todavia no termino (o el ultimo registrado no es esta partida)
            match = obtener_match(f['match_id'], vinculo['region'])
            if not match:
                continue
            participantes = match.get('info', {}).get('participants', [])
            ganador_p = next((p for p in participantes if p.get('win')), None)
            if not ganador_p:
                continue
            ganador = '1' if ganador_p.get('teamId') == 100 else '2'
            _pagar_apuestas_pendientes('flex', f['match_id'], ganador)
            fila = dict(f)
            fila['estado'] = 'resuelta'
            fila['ganador'] = ganador
            _actualizar_fila(ws_f, PARTIDA_FLEX_HEADERS, i, fila)
    except Exception as e:
        print(f'[scary_coins] Error en resolver_apuestas_loop: {e}')


@tasks.loop(minutes=15)
async def revisar_logros_loop():
    try:
        for v in _todos_los_vinculos():
            if not v.get('puuid'):
                continue
            _revisar_logros_de_cuenta(v['discord_id'], v['nombre_discord'], v['puuid'], v['region'],
                                       profundo=False)
    except Exception as e:
        print(f'[scary_coins] Error en revisar_logros_loop: {e}')


@client.event
async def on_ready():
    try:
        guild_obj = discord.Object(id=int(DISCORD_GUILD_ID))
        tree.copy_global_to(guild=guild_obj)
        synced = await tree.sync(guild=guild_obj)
        print(f'[scary_coins] {len(synced)} comandos sincronizados al instante en el servidor {DISCORD_GUILD_ID}')
    except Exception as e:
        print(f'[scary_coins] Error sincronizando comandos al guild, uso sync global: {e}')
        await tree.sync()
    if not resolver_apuestas_loop.is_running():
        resolver_apuestas_loop.start()
    if not revisar_logros_loop.is_running():
        revisar_logros_loop.start()
    print(f'[scary_coins] Conectado como {client.user}')


if __name__ == '__main__':
    if not DISCORD_TOKEN:
        raise SystemExit('Falta la variable de entorno DISCORD_TOKEN')
    client.run(DISCORD_TOKEN)
