"""
SoloQ Personalizadas Bot
-------------------------
Bot companero del torneo-lp-bot, dedicado a organizar partidas personalizadas (customs) entre
los miembros del servidor: cola de anotacion con roles (incluyendo autofill), armado de equipos
balanceados por Elo, carga de resultados, ranking y perfil de desempeno en personalizadas.

Comparte el mismo Google Sheet y la misma Riot API key que el bot del torneo (variables de
entorno GOOGLE_CREDENTIALS_JSON, GOOGLE_SHEET_NAME, RIOT_API_KEY), pero usa sus propias hojas
para no tocar los datos del SoloQ Challenge:
  - personalizadas_cola      -> quien esta anotado ahora mismo
  - personalizadas_historial -> una fila por partida jugada (equipos, resultado, fecha)
  - personalizadas_stats     -> acumulado por jugador (partidas, victorias, autofill, rol frecuente)

Nota sobre datos automaticos: la API de Riot NO permite leer el resultado de una partida
personalizada comun (solo partidas creadas con la Tournament API, que requiere aplicacion y
aprobacion aparte). Por eso el resultado se carga manualmente con /resultado_personalizada.
"""

import discord
from discord import app_commands
import requests
import json
import os
import time
import random
import itertools
import datetime
from threading import Lock

HTTP_SESSION = requests.Session()

DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
RIOT_API_KEY = os.environ.get('RIOT_API_KEY')
DISCORD_GUILD_ID = os.environ.get('DISCORD_GUILD_ID', '331997851355709451')

GOOGLE_SHEET_NAME = os.environ.get('GOOGLE_SHEET_NAME', 'SoloQ Challenge DB')
GOOGLE_CREDENTIALS_JSON = os.environ.get('GOOGLE_CREDENTIALS_JSON', '')

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
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

ROLES_LOL = ['Top', 'Jungla', 'Mid', 'ADC', 'Support']

TIER_VALOR = {
    'IRON': 0, 'BRONZE': 400, 'SILVER': 800, 'GOLD': 1200, 'PLATINUM': 1600,
    'EMERALD': 2000, 'DIAMOND': 2400, 'MASTER': 2800, 'GRANDMASTER': 3200, 'CHALLENGER': 3600,
}
RANK_VALOR = {'IV': 0, 'III': 100, 'II': 200, 'I': 300}


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
        ws = sheet.add_worksheet(title=nombre, rows=1000, cols=max(20, len(headers)))
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


COLA_HEADERS = ['discord_id', 'nombre', 'rol_principal', 'rol_secundario1', 'rol_secundario2', 'elo', 'hora_anotado']
HISTORIAL_HEADERS = ['fecha', 'equipo_a', 'equipo_b', 'ganador', 'reportado_por']
STATS_HEADERS = ['discord_id', 'nombre', 'partidas', 'victorias', 'derrotas', 'veces_autofill',
                  'rol_top', 'rol_jungla', 'rol_mid', 'rol_adc', 'rol_support', 'racha_actual', 'mejor_racha']


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


def _borrar_fila(ws, fila_index):
    _con_reintentos(lambda: ws.delete_rows(fila_index + 2))


# ---------------------------------------------------------------------------
# Riot API
# ---------------------------------------------------------------------------

def _riot_get_con_reintento(url, headers, timeout=8, intentos=3):
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


def obtener_info_ranked(riot_id, region):
    """Devuelve dict con tier/rank/lp/wins/losses/elo_num del jugador, o None si no se pudo obtener."""
    plataforma = PLATFORM_MAP.get(region.lower())
    region_base = REGION_MAP.get(region.lower())
    if not plataforma or not region_base or '#' not in riot_id:
        return None
    game_name, tag_line = riot_id.split('#', 1)
    headers = {'X-Riot-Token': RIOT_API_KEY}
    try:
        url = f'https://{region_base}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}'
        r = _riot_get_con_reintento(url, headers)
        if r.status_code != 200:
            return None
        cuenta = r.json()
        puuid = cuenta['puuid']
        nombre_completo = f"{cuenta['gameName']}#{cuenta['tagLine']}"

        time.sleep(0.05)
        url2 = f'https://{plataforma}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}'
        r2 = _riot_get_con_reintento(url2, headers)
        if r2.status_code != 200:
            return None
        resultado = {'puuid': puuid, 'nombre': nombre_completo, 'tier': 'UNRANKED', 'rank': '',
                     'lp': 0, 'wins': 0, 'losses': 0}
        for entry in r2.json():
            if entry['queueType'] == 'RANKED_SOLO_5x5':
                resultado.update({'tier': entry['tier'], 'rank': entry['rank'], 'lp': entry['leaguePoints'],
                                   'wins': entry['wins'], 'losses': entry['losses']})
                break
        resultado['elo_num'] = TIER_VALOR.get(resultado['tier'], 0) + RANK_VALOR.get(resultado['rank'], 0) + resultado['lp']
        return resultado
    except Exception as e:
        print(f'[personalizadas] Error obteniendo info ranked de {riot_id}: {e}')
        return None


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

@tree.command(name='anotarme', description='Anotate a la cola de la proxima personalizada')
@app_commands.describe(
    rol_principal='Tu linea principal para esta personalizada',
    riot_id='Tu Riot ID, formato Nombre#TAG (para calcular tu elo)',
    region='Tu region (lan, na, euw, etc.)',
    rol_secundario1='Linea secundaria que tambien jugarias (opcional)',
    rol_secundario2='Otra linea secundaria (opcional)',
)
@app_commands.choices(rol_principal=[app_commands.Choice(name=r, value=r) for r in ROLES_LOL])
@app_commands.choices(rol_secundario1=[app_commands.Choice(name=r, value=r) for r in ROLES_LOL])
@app_commands.choices(rol_secundario2=[app_commands.Choice(name=r, value=r) for r in ROLES_LOL])
async def anotarme(interaction: discord.Interaction, rol_principal: app_commands.Choice[str],
                    riot_id: str, region: str,
                    rol_secundario1: app_commands.Choice[str] = None,
                    rol_secundario2: app_commands.Choice[str] = None):
    await interaction.response.defer(ephemeral=True)
    info = obtener_info_ranked(riot_id, region)
    if info is None:
        await interaction.followup.send(
            'No pude encontrar esa cuenta de Riot. Revisa el Riot ID (Nombre#TAG) y la region.', ephemeral=True)
        return

    ws, filas = _leer_tabla('personalizadas_cola', COLA_HEADERS)
    discord_id = str(interaction.user.id)
    for i, f in enumerate(filas):
        if f['discord_id'] == discord_id:
            await interaction.followup.send('Ya estas anotado en la cola. Usa /salir_cola si te querés bajar.',
                                             ephemeral=True)
            return

    fila = {
        'discord_id': discord_id, 'nombre': info['nombre'], 'rol_principal': rol_principal.value,
        'rol_secundario1': rol_secundario1.value if rol_secundario1 else '',
        'rol_secundario2': rol_secundario2.value if rol_secundario2 else '',
        'elo': info['elo_num'], 'hora_anotado': datetime.datetime.utcnow().isoformat(),
    }
    _escribir_fila(ws, COLA_HEADERS, fila)
    total = len(filas) + 1
    await interaction.followup.send(
        f'Anotado como **{rol_principal.value}** ({info["tier"]} {info["rank"]}, {info["lp"]} LP). '
        f'Van {total} en la cola. Cuando lleguen a 10, cualquiera puede correr /armar_equipos.', ephemeral=True)


@tree.command(name='salir_cola', description='Salite de la cola de personalizadas')
async def salir_cola(interaction: discord.Interaction):
    ws, filas = _leer_tabla('personalizadas_cola', COLA_HEADERS)
    discord_id = str(interaction.user.id)
    for i, f in enumerate(filas):
        if f['discord_id'] == discord_id:
            _borrar_fila(ws, i)
            await interaction.response.send_message('Listo, te sacamos de la cola.', ephemeral=True)
            return
    await interaction.response.send_message('No estabas anotado.', ephemeral=True)


@tree.command(name='cola', description='Ver quien esta anotado para la proxima personalizada')
async def cola(interaction: discord.Interaction):
    _, filas = _leer_tabla('personalizadas_cola', COLA_HEADERS)
    if not filas:
        await interaction.response.send_message('La cola esta vacia. Anotate con /anotarme.', ephemeral=True)
        return
    lineas = []
    for i, f in enumerate(filas):
        secundarios = '/'.join([r for r in [f['rol_secundario1'], f['rol_secundario2']] if r])
        extra = f' (tambien: {secundarios})' if secundarios else ''
        lineas.append(f"{i + 1}. **{f['nombre']}** - {f['rol_principal']}{extra}")
    embed = discord.Embed(title=f'Cola de personalizadas ({len(filas)}/10)', description='\n'.join(lineas),
                           color=0x5865F2)
    await interaction.response.send_message(embed=embed)


def _balancear_equipos(jugadores):
    """Recibe una lista de 10 dicts (con elo y roles) de la gente disponible (la que esta en cola)
    y devuelve (equipo_a, equipo_b) balanceados por elo total.

    En vez de probar combinaciones al azar (que puede no encontrar la mejor), se prueban las 126
    particiones unicas posibles de 10 en dos grupos de 5 (fijando siempre al jugador 0 en un lado
    para no contar cada particion dos veces) y se queda con la de menor diferencia de elo. Si hay
    varias igual de parejas, elige una al azar entre esas para que no siempre salga la misma
    combinacion con el mismo grupo de 10 personas."""
    indices = list(range(10))
    mejor_diff = None
    mejores_splits = []
    for combo in itertools.combinations(indices[1:], 4):
        a_idx = (0,) + combo
        b_idx = tuple(i for i in indices if i not in a_idx)
        elo_a = sum(jugadores[i]['elo'] for i in a_idx)
        elo_b = sum(jugadores[i]['elo'] for i in b_idx)
        diff = abs(elo_a - elo_b)
        if mejor_diff is None or diff < mejor_diff:
            mejor_diff = diff
            mejores_splits = [(a_idx, b_idx)]
        elif diff == mejor_diff:
            mejores_splits.append((a_idx, b_idx))
    a_idx, b_idx = random.choice(mejores_splits)
    return [jugadores[i] for i in a_idx], [jugadores[i] for i in b_idx]


def _asignar_roles_equipo(equipo):
    """Asigna a cada jugador del equipo una linea de las 5, priorizando su rol principal;
    si hay choque, usa secundarios; si no alcanza, autofill. Devuelve lista de (jugador, rol_asignado, fue_autofill)."""
    disponibles = ROLES_LOL[:]
    asignados = []
    pendientes = equipo[:]
    # primera pasada: rol principal
    for j in pendientes[:]:
        if j['rol_principal'] in disponibles:
            disponibles.remove(j['rol_principal'])
            asignados.append((j, j['rol_principal'], False))
            pendientes.remove(j)
    # segunda pasada: secundarios
    for j in pendientes[:]:
        for sec in [j['rol_secundario1'], j['rol_secundario2']]:
            if sec and sec in disponibles:
                disponibles.remove(sec)
                asignados.append((j, sec, False))
                pendientes.remove(j)
                break
    # resto: autofill con lo que quede
    for j in pendientes:
        rol = disponibles.pop(0) if disponibles else '?'
        asignados.append((j, rol, True))
    orden = {r: idx for idx, r in enumerate(ROLES_LOL)}
    asignados.sort(key=lambda x: orden.get(x[1], 99))
    return asignados


@tree.command(name='armar_equipos', description='Arma dos equipos balanceados con los 10 primeros de la cola')
async def armar_equipos(interaction: discord.Interaction):
    ws, filas = _leer_tabla('personalizadas_cola', COLA_HEADERS)
    if len(filas) < 10:
        await interaction.response.send_message(
            f'Faltan jugadores: hay {len(filas)}/10 anotados.', ephemeral=True)
        return
    await interaction.response.defer()
    jugadores = filas[:10]
    for j in jugadores:
        j['elo'] = int(j['elo']) if str(j['elo']).strip().isdigit() else 0

    equipo_a, equipo_b = _balancear_equipos(jugadores)
    asign_a = _asignar_roles_equipo(equipo_a)
    asign_b = _asignar_roles_equipo(equipo_b)

    def _fmt_equipo(asign):
        lineas = []
        for j, rol, autofill in asign:
            marca = ' (autofill)' if autofill else ''
            lineas.append(f'**{rol}**{marca}: {j["nombre"]}')
        return '\n'.join(lineas)

    elo_a = sum(j['elo'] for j in equipo_a)
    elo_b = sum(j['elo'] for j in equipo_b)

    embed = discord.Embed(title='Equipos armados', color=0xF1C40F,
                           description=f'Diferencia de elo entre equipos: {abs(elo_a - elo_b)} pts')
    embed.add_field(name='Equipo A', value=_fmt_equipo(asign_a), inline=True)
    embed.add_field(name='Equipo B', value=_fmt_equipo(asign_b), inline=True)
    embed.set_footer(text='Cuando termine la partida, reporten el resultado con /resultado_personalizada')
    await interaction.followup.send(embed=embed)

    # vaciar la cola (ya se usaron estos 10)
    for i in range(9, -1, -1):
        _borrar_fila(ws, i)

    # guardar quienes jugaron, para poder actualizar sus estadisticas cuando se reporte el resultado
    guild_id = str(interaction.guild_id)
    ULTIMO_EQUIPOS[guild_id] = {'equipo_a': asign_a, 'equipo_b': asign_b, 'reportado': False}


ULTIMO_EQUIPOS = {}  # guild_id -> {'equipo_a': [(jugador, rol, autofill)...], 'equipo_b': [...], 'reportado': bool}

ROL_A_HEADER = {'Top': 'rol_top', 'Jungla': 'rol_jungla', 'Mid': 'rol_mid', 'ADC': 'rol_adc', 'Support': 'rol_support'}


# ---------------------------------------------------------------------------
# Armado manual de equipos (por si alguien quiere armarlos a mano en vez de /armar_equipos)
# ---------------------------------------------------------------------------

MANUAL_DRAFTS = {}  # guild_id -> {'A': {rol: {discord_id, nombre, elo, ...}}, 'B': {...}}


def _draft_vacio():
    return {'A': {}, 'B': {}}


@tree.command(name='manual_iniciar', description='Empieza a armar los equipos a mano (borra cualquier borrador anterior)')
async def manual_iniciar(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    MANUAL_DRAFTS[guild_id] = _draft_vacio()
    await interaction.response.send_message(
        'Borrador de equipos manuales iniciado. Anda llenando los 10 cupos (2 equipos x 5 lineas) '
        'con /manual_asignar. Cuando esten los 10, usa /manual_confirmar.', ephemeral=True)


@tree.command(name='manual_asignar', description='Asigna a mano un jugador a un equipo y linea')
@app_commands.describe(
    equipo='Equipo A o B', rol='Linea a asignar', jugador='El jugador',
    riot_id='Riot ID del jugador (Nombre#TAG), opcional, para calcular su elo real',
    region='Region del jugador, obligatorio si pones riot_id',
)
@app_commands.choices(
    equipo=[app_commands.Choice(name='Equipo A', value='A'), app_commands.Choice(name='Equipo B', value='B')],
    rol=[app_commands.Choice(name=r, value=r) for r in ROLES_LOL],
)
async def manual_asignar(interaction: discord.Interaction, equipo: app_commands.Choice[str],
                          rol: app_commands.Choice[str], jugador: discord.Member,
                          riot_id: str = None, region: str = None):
    guild_id = str(interaction.guild_id)
    draft = MANUAL_DRAFTS.setdefault(guild_id, _draft_vacio())

    elo_num, nombre = 0, jugador.display_name
    if riot_id and region:
        await interaction.response.defer(ephemeral=True)
        info = obtener_info_ranked(riot_id, region)
        if info:
            elo_num, nombre = info['elo_num'], info['nombre']
        enviar = interaction.followup.send
    else:
        enviar = interaction.response.send_message

    # si ya estaba puesto en otro cupo, lo sacamos de ahi primero
    for eq in ('A', 'B'):
        for r in list(draft[eq].keys()):
            if draft[eq][r]['discord_id'] == str(jugador.id):
                del draft[eq][r]

    draft[equipo.value][rol.value] = {
        'discord_id': str(jugador.id), 'nombre': nombre, 'elo': elo_num,
        'rol_principal': rol.value, 'rol_secundario1': '', 'rol_secundario2': '',
    }
    total = sum(len(draft[eq]) for eq in ('A', 'B'))
    await enviar(f'{jugador.display_name} puesto en **Equipo {equipo.value} - {rol.value}**. '
                 f'Van {total}/10. Cuando esten los 10, usa /manual_confirmar.', ephemeral=True)


@tree.command(name='manual_ver', description='Ver el borrador actual de equipos manuales')
async def manual_ver(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    draft = MANUAL_DRAFTS.get(guild_id, _draft_vacio())

    def _fmt(eq):
        return '\n'.join(f"**{r}**: {draft[eq][r]['nombre']}" if r in draft[eq] else f'**{r}**: _vacio_'
                          for r in ROLES_LOL)

    embed = discord.Embed(title='Borrador de equipos manuales', color=0x95A5A6)
    embed.add_field(name='Equipo A', value=_fmt('A'), inline=True)
    embed.add_field(name='Equipo B', value=_fmt('B'), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name='manual_quitar', description='Saca a un jugador del borrador manual')
@app_commands.describe(jugador='El jugador a sacar del borrador')
async def manual_quitar(interaction: discord.Interaction, jugador: discord.Member):
    guild_id = str(interaction.guild_id)
    draft = MANUAL_DRAFTS.setdefault(guild_id, _draft_vacio())
    sacado = False
    for eq in ('A', 'B'):
        for r in list(draft[eq].keys()):
            if draft[eq][r]['discord_id'] == str(jugador.id):
                del draft[eq][r]
                sacado = True
    await interaction.response.send_message(
        'Sacado del borrador.' if sacado else 'Ese jugador no estaba en el borrador.', ephemeral=True)


@tree.command(name='manual_confirmar', description='Confirma los equipos armados a mano (deben estar los 10 cupos llenos)')
async def manual_confirmar(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    draft = MANUAL_DRAFTS.get(guild_id, _draft_vacio())
    faltantes = [f'Equipo {eq} - {r}' for eq in ('A', 'B') for r in ROLES_LOL if r not in draft[eq]]
    if faltantes:
        await interaction.response.send_message(
            'Todavia faltan cupos por llenar: ' + ', '.join(faltantes), ephemeral=True)
        return

    asign_a = [(draft['A'][r], r, False) for r in ROLES_LOL]
    asign_b = [(draft['B'][r], r, False) for r in ROLES_LOL]

    def _fmt_equipo(asign):
        return '\n'.join(f'**{rol}**: {j["nombre"]}' for j, rol, _ in asign)

    elo_a = sum(j['elo'] for j, _, _ in asign_a)
    elo_b = sum(j['elo'] for j, _, _ in asign_b)
    embed = discord.Embed(title='Equipos armados (manual)', color=0x95A5A6,
                           description=f'Diferencia de elo entre equipos: {abs(elo_a - elo_b)} pts')
    embed.add_field(name='Equipo A', value=_fmt_equipo(asign_a), inline=True)
    embed.add_field(name='Equipo B', value=_fmt_equipo(asign_b), inline=True)
    embed.set_footer(text='Cuando termine la partida, reporten el resultado con /resultado_personalizada')
    await interaction.response.send_message(embed=embed)

    ULTIMO_EQUIPOS[guild_id] = {'equipo_a': asign_a, 'equipo_b': asign_b, 'reportado': False}
    MANUAL_DRAFTS[guild_id] = _draft_vacio()


def _actualizar_stats_jugador(ws, filas, jugador, rol, autofill, gano):
    """Busca (o crea) la fila de stats de un jugador y aplica el resultado de una partida."""
    discord_id = jugador.get('discord_id', '')
    idx = next((i for i, f in enumerate(filas) if f['discord_id'] == discord_id), None)
    if idx is None:
        fila = {h: 0 for h in STATS_HEADERS}
        fila['discord_id'] = discord_id
        fila['nombre'] = jugador.get('nombre', '')
    else:
        fila = dict(filas[idx])

    def _n(campo):
        try:
            return int(fila.get(campo, 0) or 0)
        except (TypeError, ValueError):
            return 0

    fila['partidas'] = _n('partidas') + 1
    if gano:
        fila['victorias'] = _n('victorias') + 1
        fila['racha_actual'] = _n('racha_actual') + 1
    else:
        fila['derrotas'] = _n('derrotas') + 1
        fila['racha_actual'] = 0
    fila['mejor_racha'] = max(_n('mejor_racha'), _n('racha_actual'))
    if autofill:
        fila['veces_autofill'] = _n('veces_autofill') + 1
    header_rol = ROL_A_HEADER.get(rol)
    if header_rol:
        fila[header_rol] = _n(header_rol) + 1

    if idx is None:
        _escribir_fila(ws, STATS_HEADERS, fila)
        filas.append(fila)
    else:
        _actualizar_fila(ws, STATS_HEADERS, idx, fila)
        filas[idx] = fila


@tree.command(name='resultado_personalizada', description='Reporta el resultado de la ultima personalizada armada')
@app_commands.describe(ganador='Que equipo gano')
@app_commands.choices(ganador=[app_commands.Choice(name='Equipo A', value='A'),
                                app_commands.Choice(name='Equipo B', value='B')])
async def resultado_personalizada(interaction: discord.Interaction, ganador: app_commands.Choice[str]):
    await interaction.response.defer()
    guild_id = str(interaction.guild_id)
    datos = ULTIMO_EQUIPOS.get(guild_id)

    equipo_a_str, equipo_b_str = '', ''
    if datos and not datos['reportado']:
        equipo_a_str = ', '.join(j['nombre'] for j, _, _ in datos['equipo_a'])
        equipo_b_str = ', '.join(j['nombre'] for j, _, _ in datos['equipo_b'])

    ws_hist, _ = _leer_tabla('personalizadas_historial', HISTORIAL_HEADERS)
    fila = {
        'fecha': datetime.datetime.utcnow().isoformat(),
        'equipo_a': equipo_a_str, 'equipo_b': equipo_b_str, 'ganador': ganador.value,
        'reportado_por': str(interaction.user.id),
    }
    _escribir_fila(ws_hist, HISTORIAL_HEADERS, fila)

    nota_extra = ''
    if datos and not datos['reportado']:
        ws_stats, filas_stats = _leer_tabla('personalizadas_stats', STATS_HEADERS)
        for j, rol, autofill in datos['equipo_a']:
            _actualizar_stats_jugador(ws_stats, filas_stats, j, rol, autofill, gano=(ganador.value == 'A'))
        for j, rol, autofill in datos['equipo_b']:
            _actualizar_stats_jugador(ws_stats, filas_stats, j, rol, autofill, gano=(ganador.value == 'B'))
        datos['reportado'] = True
    else:
        nota_extra = ('\n(No encontre el ultimo /armar_equipos de este servidor, asi que no pude actualizar '
                       'estadisticas individuales. Reporten justo despues de armar los equipos.)')

    await interaction.followup.send(
        f'Resultado cargado: gano el **Equipo {ganador.value}**. Gracias por reportar.{nota_extra}')


@tree.command(name='elo', description='Consulta el elo actual de una cuenta de Riot')
@app_commands.describe(riot_id='Riot ID, formato Nombre#TAG', region='Region (lan, na, euw, etc.)')
async def elo(interaction: discord.Interaction, riot_id: str, region: str):
    await interaction.response.defer(ephemeral=True)
    info = obtener_info_ranked(riot_id, region)
    if info is None:
        await interaction.followup.send('No pude encontrar esa cuenta. Revisa el Riot ID y la region.',
                                         ephemeral=True)
        return
    embed = discord.Embed(title=info['nombre'], color=0x1ABC9C)
    embed.add_field(name='Rango', value=f"{info['tier']} {info['rank']} ({info['lp']} LP)", inline=True)
    embed.add_field(name='Record', value=f"{info['wins']}W - {info['losses']}L", inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name='ranking_personalizadas', description='Ranking de desempeno en personalizadas')
async def ranking_personalizadas(interaction: discord.Interaction):
    _, filas = _leer_tabla('personalizadas_stats', STATS_HEADERS)
    if not filas:
        await interaction.response.send_message('Todavia no hay partidas registradas.', ephemeral=True)
        return

    def _wr(f):
        p = int(f['partidas'] or 0)
        return (int(f['victorias'] or 0) / p) if p else 0

    filas.sort(key=lambda f: (int(f['partidas'] or 0), _wr(f)), reverse=True)
    lineas = []
    for i, f in enumerate(filas[:15]):
        p = int(f['partidas'] or 0)
        v = int(f['victorias'] or 0)
        wr = round(_wr(f) * 100)
        lineas.append(f"{i + 1}. **{f['nombre']}** - {p} jugadas, {v}W ({wr}%)")
    embed = discord.Embed(title='Ranking de personalizadas', description='\n'.join(lineas), color=0xE67E22)
    await interaction.response.send_message(embed=embed)


@tree.command(name='perfil_personalizadas', description='Ver tu desempeno (o el de otro) en personalizadas')
@app_commands.describe(usuario='De quien queres ver el perfil (opcional)')
async def perfil_personalizadas(interaction: discord.Interaction, usuario: discord.Member = None):
    objetivo = usuario or interaction.user
    _, filas = _leer_tabla('personalizadas_stats', STATS_HEADERS)
    fila = next((f for f in filas if f['discord_id'] == str(objetivo.id)), None)
    if fila is None:
        await interaction.response.send_message(f'{objetivo.display_name} todavia no tiene partidas registradas.',
                                                  ephemeral=True)
        return
    roles_conteo = {r: int(fila.get(f'rol_{r.lower()}', 0) or 0) for r in
                    ['Top', 'Jungla', 'Mid', 'Adc', 'Support']}
    rol_favorito = max(roles_conteo, key=roles_conteo.get) if any(roles_conteo.values()) else '-'
    embed = discord.Embed(title=f'Perfil de personalizadas - {objetivo.display_name}', color=0x9B59B6)
    embed.add_field(name='Partidas', value=fila.get('partidas', 0), inline=True)
    embed.add_field(name='Victorias', value=fila.get('victorias', 0), inline=True)
    embed.add_field(name='Derrotas', value=fila.get('derrotas', 0), inline=True)
    embed.add_field(name='Veces en autofill', value=fila.get('veces_autofill', 0), inline=True)
    embed.add_field(name='Rol favorito', value=rol_favorito, inline=True)
    embed.add_field(name='Mejor racha', value=fila.get('mejor_racha', 0), inline=True)
    await interaction.response.send_message(embed=embed)


@client.event
async def on_ready():
    # Sync GLOBAL puede tardar hasta 1 hora en aparecerle a los usuarios. Sincronizamos directo
    # al servidor (guild) para que los comandos (incluido /anotarme) aparezcan al instante.
    try:
        guild_obj = discord.Object(id=int(DISCORD_GUILD_ID))
        tree.copy_global_to(guild=guild_obj)
        synced = await tree.sync(guild=guild_obj)
        print(f'[personalizadas] {len(synced)} comandos sincronizados al instante en el servidor {DISCORD_GUILD_ID}')
    except Exception as e:
        print(f'[personalizadas] Error sincronizando comandos al guild, uso sync global: {e}')
        await tree.sync()
    print(f'[personalizadas] Conectado como {client.user}')


if __name__ == '__main__':
    if not DISCORD_TOKEN:
        raise SystemExit('Falta la variable de entorno DISCORD_TOKEN')
    client.run(DISCORD_TOKEN)
