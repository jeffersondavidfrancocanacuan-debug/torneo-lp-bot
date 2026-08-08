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

LOGROS = {
    'lp_50':   {'nombre': '+50 LP',  'desc': 'Alcanzo 50 puntos netos'},
    'lp_100':  {'nombre': '+100 LP', 'desc': 'Alcanzo 100 puntos netos'},
    'lp_200':  {'nombre': '+200 LP', 'desc': 'Alcanzo 200 puntos netos'},
    'ascenso': {'nombre': 'Ascenso', 'desc': 'Subio de division desde el registro'},
    'top1':    {'nombre': 'Cima',    'desc': 'Llego al puesto #1 de su categoria'},
    'top3':    {'nombre': 'Podio',   'desc': 'Entro al top 3 de su categoria'},
}

# ------------------- ESCUDOS AZULES (maldiciones estilo soloqchallenge.gg) -------------------
MALDICION_COOLDOWN_HORAS = 24
MALDICION_MAX_ACTIVAS = 3
MALDICION_DURACION_HORAS = 24

EFECTOS_MALDICION = [
    'Hechizos de invocador obligatorios: solo Flash + Ignite en tu proxima partida.',
    'Campeon aleatorio obligatorio (usa una ruleta random) en tu proxima partida.',
    'Rol/posicion aleatoria obligatoria en tu proxima partida.',
    'Debes banear el campeon que te pida el que te maldijo en tu proxima partida.',
    'COMODIN - Rebote: la maldicion vuelve a quien te la lanzo.',
    'COMODIN - Rebote al azar: la maldicion salta a otro jugador random del torneo.',
]

# Sesiones de voz activas en memoria: {discord_id: datetime_de_ultimo_checkpoint}
VOICE_SESIONES = {}



# ------------------- PERSISTENCIA (Google Sheets) -------------------

GOOGLE_SHEET_NAME = os.environ.get('GOOGLE_SHEET_NAME', 'SoloQ Challenge DB')
GOOGLE_CREDENTIALS_JSON = os.environ.get('GOOGLE_CREDENTIALS_JSON', '')

JUGADORES_HEADERS = [
    'puuid', 'discord_id', 'nombre', 'region', 'lp_inicial', 'tier_inicial', 'rank_inicial',
    'elo', 'estado', 'fecha_registro', 'bonus_total', 'castigos_total', 'logros',
    'tiempo_voz_min', 'elo_previo', 'escudos', 'maldiciones', 'ultimo_escudo_uso',
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
            db[puuid] = {
                'discord_id': f.get('discord_id', ''),
                'nombre': f.get('nombre', ''),
                'region': f.get('region', ''),
                'lp_inicial': int(float(f.get('lp_inicial') or 0)),
                'tier_inicial': f.get('tier_inicial', ''),
                'rank_inicial': f.get('rank_inicial', ''),
                'elo': f.get('elo') or 'low',
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
            }
        meta_ws = _get_or_create_worksheet('meta', META_HEADERS)
        for fila in meta_ws.get_all_records(numericise_ignore=['all']):
            clave = fila.get('clave')
            valor = fila.get('valor')
            if clave == 'inicio_torneo' and valor:
                db['inicio_torneo'] = valor
            elif clave == 'torneo_iniciado':
                db['torneo_iniciado'] = str(valor).strip().lower() in ('true', '1', 'si', 'si')
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
                      ['torneo_iniciado', 'True' if db.get('torneo_iniciado') else 'False']]
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


def jugadores_validos(db):
    return {k: v for k, v in db.items() if k not in ('inicio_torneo', 'torneo_iniciado') and isinstance(v, dict)}


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


def tiempo_restante_cooldown(data):
    ultimo = data.get('ultimo_escudo_uso')
    if not ultimo:
        return 0
    try:
        fecha = datetime.datetime.fromisoformat(ultimo)
    except Exception:
        return 0
    transcurridas = (datetime.datetime.now() - fecha).total_seconds() / 3600
    return max(MALDICION_COOLDOWN_HORAS - transcurridas, 0)


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


def determinar_elo(tier):
    high_tiers = ['MASTER', 'GRANDMASTER', 'CHALLENGER']
    return 'high' if tier.upper() in high_tiers else 'low'


TIER_ORDEN = ['IRON', 'BRONZE', 'SILVER', 'GOLD', 'PLATINUM', 'EMERALD',
              'DIAMOND', 'MASTER', 'GRANDMASTER', 'CHALLENGER']


def tier_index(tier):
    try:
        return TIER_ORDEN.index(tier.upper())
    except ValueError:
        return -1


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
    """Devuelve (high, low, pendientes, sin_voz) con los datos ya frescos de Riot."""
    high, low, pendientes, sin_voz = [], [], [], []
    for puuid, data in jugadores_validos(db).items():
        info = obtener_info_ranked(data['nombre'], data['region'])
        if info is None:
            continue
        lp_ganados = info['lp'] - data['lp_inicial']
        total = lp_ganados + data.get('bonus_total', 0) - data.get('castigos_total', 0)
        tiempo_voz = round(data.get('tiempo_voz_min', 0), 1)
        jugador = {
            'puuid': puuid,
            'discord_id': data['discord_id'],
            'nombre': data['nombre'],
            'lp_ganados': lp_ganados,
            'lp_actual': info['lp'],
            'tier_actual': info['tier'],
            'rank_actual': info['rank'],
            'tier_inicial': data.get('tier_inicial', info['tier']),
            'elo': data.get('elo', 'low'),
            'bonus': data.get('bonus_total', 0),
            'castigos': data.get('castigos_total', 0),
            'total': total,
            'estado': data.get('estado', 'aprobado'),
            'tiempo_voz_min': tiempo_voz,
            'voz_verificado': tiempo_voz >= VOZ_MINIMA_MINUTOS,
            'elo_previo': data.get('elo_previo', ''),
            'escudos': data.get('escudos', 0),
        }
        if jugador['estado'] == 'pendiente':
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
                data['escudos'] = data.get('escudos', 0) + len(recien_desbloqueados)
                for clave in recien_desbloqueados:
                    info_logro = LOGROS.get(clave)
                    if info_logro:
                        anuncios.append(f"{info_logro['nombre']} - **{j['nombre']}** {info_logro['desc']} ({'High' if categoria == 'high' else 'Low'} Elo) (+1 Escudo Azul)")

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
                        elo_previo='Opcional: tu elo mas alto alcanzado antes (ej. Master, Diamond). Ayuda a la directiva a clasificarte si tu cuenta es nueva.')
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

    total_partidas = info['wins'] + info['losses']
    estado = 'pendiente' if total_partidas < JUEGOS_MINIMOS_CUENTA else 'aprobado'

    ahora = datetime.datetime.now()
    db[info['puuid']] = {
        'discord_id': user_id,
        'nombre': info['nombre'],
        'region': region,
        'lp_inicial': info['lp'],
        'tier_inicial': info['tier'],
        'rank_inicial': info['rank'],
        'elo': determinar_elo(info['tier']),
        'estado': estado,
        'fecha_registro': str(ahora),
        'bonus_total': 0,
        'castigos_total': 0,
        'logros': [],
        'tiempo_voz_min': 0,
        'elo_previo': elo_previo,
        'escudos': 0,
        'maldiciones': [],
        'ultimo_escudo_uso': None,
    }
    if 'inicio_torneo' not in db:
        db['inicio_torneo'] = str(ahora)
    guardar_db(db)

    categoria_txt = "High Elo" if db[info["puuid"]]["elo"] == "high" else "Low Elo"
    elo_previo_txt = f'\nElo previo declarado: **{elo_previo}** (la directiva lo vera al revisar tu cuenta).' if elo_previo else ''
    if estado == 'pendiente':
        await interaction.followup.send(
            f'{interaction.user.mention} registrado como **{info["nombre"]}** (LAN).\n'
            f'Tu cuenta tiene solo {total_partidas} partidas en soloQ, por lo que queda **pendiente de revision** '
            f'por la directiva antes de aparecer en la tabla (posible cuenta nueva/comprada).\n'
            f'Categoria sugerida: {categoria_txt}.{elo_previo_txt}\n'
            f'Recuerda: ademas de la aprobacion, tus puntos solo son validos si te conectas al chat de voz del servidor (cualquier canal).'
        )
    else:
        await interaction.followup.send(
            f'{interaction.user.mention} registrado como **{info["nombre"]}** (LAN).\n'
            f'LP inicial: {info["lp"]} ({info["tier"]} {info["rank"]}).\n'
            f'Categoria: {categoria_txt}.\n'
            f'Importante: para que tus puntos sean validos debes conectarte al chat de voz del servidor (cualquier canal) mientras juegas.\n'
            f'A jugar! El torneo dura {DURACION_TORNEO} dias.'
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
            lp_ganados = info['lp'] - data['lp_inicial']
            total = lp_ganados + data.get('bonus_total', 0) - data.get('castigos_total', 0)
            estado_txt = 'Pendiente de revision' if data.get('estado') == 'pendiente' else 'Aprobado'
            tiempo_voz = round(data.get('tiempo_voz_min', 0), 1)
            voz_txt = f'{tiempo_voz} min (verificado)' if tiempo_voz >= VOZ_MINIMA_MINUTOS else f'{tiempo_voz} min (necesitas {round(VOZ_MINIMA_MINUTOS - tiempo_voz, 1)} min mas conectado a voz para que tus puntos cuenten)'
            await interaction.followup.send(
                f'**{data["nombre"]}** ({estado_txt})\n'
                f'LP inicial: {data["lp_inicial"]} ({data["tier_inicial"]} {data["rank_inicial"]})\n'
                f'LP actual: {info["lp"]} ({info["tier"]} {info["rank"]})\n'
                f'LP ganados: {lp_ganados}\n'
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

    if objetivo['estado'] == 'pendiente':
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
    embed.add_field(name='Categoria', value='High Elo' if objetivo['elo'] == 'high' else 'Low Elo', inline=True)
    embed.add_field(name='Posicion', value=posicion_txt, inline=True)
    embed.add_field(name='Rango actual', value=f'{objetivo["tier_actual"]} {objetivo["rank_actual"]}', inline=True)
    embed.add_field(name='LP ganados', value=str(objetivo['lp_ganados']), inline=True)

    embed.add_field(name='Bonus', value=f'+{objetivo["bonus"]}', inline=True)
    embed.add_field(name='Castigos', value=f'-{objetivo["castigos"]}', inline=True)
    embed.add_field(name='Tiempo en voz', value=f'{objetivo["tiempo_voz_min"]} min', inline=True)
    embed.add_field(name='Total', value=f'**{objetivo["total"]} pts**', inline=True)
    embed.add_field(name='Escudos Azules', value=str(objetivo.get('escudos', 0)), inline=True)
    if objetivo.get('elo_previo'):
        embed.add_field(name='Elo previo declarado', value=objetivo['elo_previo'], inline=True)
    activas = maldiciones_activas_de(data)
    malds_txt = '\n'.join(f'- {m["efecto"]}' for m in activas) or 'Ninguna'
    embed.add_field(name=f'Maldiciones activas ({len(activas)}/{MALDICION_MAX_ACTIVAS})', value=malds_txt, inline=False)
    embed.add_field(name='Logros', value=logros_txt, inline=False)
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
                value=f'{mention} -> {j["total"]} pts (LP: {j["lp_ganados"]}, Bonus: +{j["bonus"]}, Castigos: -{j["castigos"]}, Voz: {j["tiempo_voz_min"]} min)',
                inline=False
            )
    if low:
        embed.add_field(name='Low Elo (Hierro - Diamante)', value='​', inline=False)
        for i, j in enumerate(low, 1):
            user = canal.guild.get_member(int(j['discord_id']))
            mention = user.mention if user else j['nombre']
            embed.add_field(
                name=f'{i}. {j["nombre"]}',
                value=f'{mention} -> {j["total"]} pts (LP: {j["lp_ganados"]}, Bonus: +{j["bonus"]}, Castigos: -{j["castigos"]}, Voz: {j["tiempo_voz_min"]} min)',
                inline=False
            )
    if sin_voz:
        embed.add_field(name='Sin verificar (falta chat de voz)', value='​', inline=False)
        for j in sin_voz:
            faltante = round(VOZ_MINIMA_MINUTOS - j['tiempo_voz_min'], 1)
            embed.add_field(name=j['nombre'], value=f'Conectado {j["tiempo_voz_min"]} min - le faltan {faltante} min en voz', inline=False)
    if pendientes:
        embed.add_field(name='Pendientes de revision (cuenta nueva)', value='​', inline=False)
        for j in pendientes:
            embed.add_field(name=j['nombre'], value='Esperando aprobacion de la directiva (`/clasificar`)', inline=False)
    embed.set_footer(text=f'{calcular_estado_torneo(db)} - Web: ver enlace fijado')
    await canal.send(embed=embed)
    await procesar_logros_y_roles(canal, high, low, db)


@tree.command(name='ayuda', description='Muestra todos los comandos disponibles')
async def ayuda(interaction: discord.Interaction):
    embed = discord.Embed(title='SoloQ Challenge - Comandos', color=0x5865F2,
                           description=calcular_estado_torneo(cargar_db()))
    embed.add_field(
        name='Jugadores',
        value=('`/registrar` - Inscribete con tu Riot ID (Nombre#TAG). Opcional: declara tu elo previo\n'
               '`/progreso` - Consulta tu propio avance de LP\n'
               '`/perfil` - Tu tarjeta completa (posicion, logros, escudos, etc.)\n'
               '`/tabla` - Muestra la clasificacion al instante\n'
               '`/escudos` - Ve tus Escudos Azules y maldiciones activas\n'
               '`/maldecir` - Gasta un Escudo Azul y maldice a otro jugador al azar'),
        inline=False
    )
    embed.add_field(
        name='Administracion',
        value=('`/bonus` - Otorga puntos extra a un jugador\n'
               '`/castigar` - Aplica una penalizacion\n'
               '`/otorgar_escudo` - Da un Escudo Azul por una hazana (Primera Sangre, Penta, etc.)\n'
               '`/historial` - Revisa el historial de cambios de un jugador\n'
               '`/pendientes` - Lista cuentas nuevas esperando revision (con elo previo declarado)\n'
               '`/clasificar` - Aprueba y asigna categoria a una cuenta pendiente\n'
               '`/iniciar_torneo` - Comienza oficialmente el torneo y reinicia el progreso de pruebas\n'
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
        value=(f'Se ganan al desbloquear logros o por hazanas dentro del juego (la directiva las otorga). '
               f'Usa `/maldecir` para gastar uno y aplicar un efecto aleatorio a otro jugador (~1 de cada 3 rebota). '
               f'Maximo {MALDICION_MAX_ACTIVAS} maldiciones activas por victima, cooldown de {MALDICION_COOLDOWN_HORAS}h por lanzador.'),
        inline=False
    )
    embed.add_field(
        name='Web',
        value='La clasificacion tambien esta disponible en la pagina web del torneo (se actualiza sola).',
        inline=False
    )
    embed.set_footer(text='Categorias: Low Elo (Hierro-Diamante) - High Elo (Master-Retador)')
    await interaction.response.send_message(embed=embed)


@tree.command(name='pendientes', description='(Admin) Lista cuentas pendientes de revision')
@app_commands.default_permissions(administrator=True)
async def pendientes(interaction: discord.Interaction):
    await interaction.response.defer()
    db = cargar_db()
    _, _, pend, _ = calcular_tabla(db)
    if not pend:
        await interaction.followup.send('No hay cuentas pendientes de revision.')
        return
    mensaje = '**Cuentas pendientes:**\n'
    for j in pend:
        extra = f' - elo previo declarado: **{j["elo_previo"]}**' if j.get('elo_previo') else ''
        mensaje += f'- **{j["nombre"]}** - {j["tier_actual"]} {j["rank_actual"]} - <@{j["discord_id"]}>{extra}\n'
    mensaje += '\nUsa `/clasificar usuario:@jugador categoria:low|high` para aprobar.'
    await interaction.followup.send(mensaje)


@tree.command(name='clasificar', description='(Admin) Aprueba una cuenta pendiente y define su categoria')
@app_commands.describe(usuario='Jugador a aprobar', categoria='low o high')
@app_commands.choices(categoria=[
    app_commands.Choice(name='Low Elo', value='low'),
    app_commands.Choice(name='High Elo', value='high'),
])
@app_commands.default_permissions(administrator=True)
async def clasificar(interaction: discord.Interaction, usuario: discord.Member, categoria: app_commands.Choice[str]):
    await interaction.response.defer()
    db = cargar_db()
    for puuid, data in jugadores_validos(db).items():
        if data['discord_id'] == str(usuario.id):
            data['estado'] = 'aprobado'
            data['elo'] = categoria.value
            guardar_db(db)
            await interaction.followup.send(
                f'**{data["nombre"]}** aprobado y clasificado en **{"High" if categoria.value == "high" else "Low"} Elo**. Ya aparece en la tabla (si cumple el requisito de voz).')
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
            malds_txt = '\n'.join(f'- {m["efecto"]} (de <@{m["de"]}>)' for m in activas) or 'Ninguna'
            restante_cd = tiempo_restante_cooldown(data)
            cd_txt = 'Disponible' if restante_cd <= 0 else f'{round(restante_cd, 1)} h restantes'
            await interaction.followup.send(
                f'**{data["nombre"]}**\n'
                f'Escudos Azules disponibles: **{data.get("escudos", 0)}**\n'
                f'Cooldown para lanzar: {cd_txt}\n'
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
    if caster_data.get('escudos', 0) <= 0:
        await interaction.followup.send('No tienes Escudos Azules disponibles. Ganalos desbloqueando logros o pidiendole uno a la directiva por una hazana.')
        return
    restante_cd = tiempo_restante_cooldown(caster_data)
    if restante_cd > 0:
        await interaction.followup.send(f'Debes esperar {round(restante_cd, 1)} horas mas para volver a lanzar una maldicion.')
        return

    efecto = random.choice(EFECTOS_MALDICION)
    es_rebote_lanzador = efecto.startswith('COMODIN - Rebote:')
    es_rebote_random = efecto.startswith('COMODIN - Rebote al azar')

    destino_puuid, destino_data = target_puuid, target_data
    if es_rebote_lanzador:
        destino_puuid, destino_data = caster_puuid, caster_data
    elif es_rebote_random:
        candidatos = [(p, d) for p, d in valid.items() if d.get('estado') == 'aprobado' and d['discord_id'] != caster_id]
        if candidatos:
            destino_puuid, destino_data = random.choice(candidatos)

    if len(maldiciones_activas_de(destino_data)) >= MALDICION_MAX_ACTIVAS:
        await interaction.followup.send(
            f'**{destino_data["nombre"]}** ya tiene el maximo de {MALDICION_MAX_ACTIVAS} maldiciones activas ahora mismo. '
            f'Intenta con otro objetivo o espera a que expiren (dura {MALDICION_DURACION_HORAS}h).')
        return

    ahora = str(datetime.datetime.now())
    caster_data['escudos'] = caster_data.get('escudos', 0) - 1
    caster_data['ultimo_escudo_uso'] = ahora
    destino_data.setdefault('maldiciones', []).append({'efecto': efecto, 'de': caster_id, 'fecha': ahora})
    guardar_db(db)

    embed = discord.Embed(title='Maldicion lanzada!', color=0x9b59b6, timestamp=datetime.datetime.now())
    embed.add_field(name='Lanzada por', value=f'<@{caster_id}>', inline=True)
    embed.add_field(name='Objetivo final', value=f'**{destino_data["nombre"]}**', inline=True)
    embed.add_field(name='Efecto', value=efecto, inline=False)
    embed.set_footer(text=f'Dura {MALDICION_DURACION_HORAS}h - Maximo {MALDICION_MAX_ACTIVAS} activas por jugador - Cooldown de lanzamiento: {MALDICION_COOLDOWN_HORAS}h')
    await interaction.followup.send(embed=embed)


@tree.command(name='otorgar_escudo', description='(Admin) Otorga un Escudo Azul por una hazana dentro del juego')
@app_commands.describe(usuario='Jugador a premiar', motivo='ej. Primera Sangre, Penta Kill, Ace')
@app_commands.default_permissions(administrator=True)
async def otorgar_escudo(interaction: discord.Interaction, usuario: discord.Member, motivo: str = ""):
    await interaction.response.defer()
    db = cargar_db()
    for puuid, data in jugadores_validos(db).items():
        if data['discord_id'] == str(usuario.id):
            data['escudos'] = data.get('escudos', 0) + 1
            guardar_db(db)
            await interaction.followup.send(
                f'**{data["nombre"]}** recibio un Escudo Azul. Motivo: {motivo or "N/A"}. Total: {data["escudos"]}.')
            return
    await interaction.followup.send('Usuario no encontrado en el torneo.')


@tree.command(name='reiniciar_registro', description='(Admin) PELIGRO: borra TODOS los registros para empezar con cuentas nuevas')
@app_commands.describe(confirmar='Escribe SI (mayusculas) para confirmar el borrado total')
@app_commands.default_permissions(administrator=True)
async def reiniciar_registro(interaction: discord.Interaction, confirmar: str):
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


@tree.command(name='iniciar_torneo', description='(Admin) Inicia oficialmente el torneo y reinicia el progreso de pruebas')
@app_commands.default_permissions(administrator=True)
async def iniciar_torneo(interaction: discord.Interaction):
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
        f'(el conteo de LP arranca desde ahora, el tiempo de voz acumulado se conserva). Dura {DURACION_TORNEO} dias.')
    if CANAL_CLASIFICACION_ID != 0:
        canal = client.get_channel(CANAL_CLASIFICACION_ID)
        if canal:
            await canal.send('**EL TORNEO HA COMENZADO OFICIALMENTE!** Buena suerte a todos.')

            await mostrar_tabla(canal)


@tree.command(name='castigar', description='(Admin) Resta puntos a un jugador')
@app_commands.describe(usuario='Jugador a castigar', puntos='Puntos a restar', motivo='Razon del castigo')
@app_commands.default_permissions(administrator=True)
async def castigar(interaction: discord.Interaction, usuario: discord.Member, puntos: int, motivo: str = ""):
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
                f'**{data["nombre"]}** ha recibido un castigo de **-{puntos} puntos**.\n'
                f'Motivo: {motivo}\nTotal castigos: -{data["castigos_total"]}')
            return
    await interaction.followup.send('Usuario no encontrado en el torneo.')


@tree.command(name='bonus', description='(Admin) Otorga puntos extra a un jugador')
@app_commands.describe(usuario='Jugador a bonificar', puntos='Puntos a sumar', motivo='Razon (ej. Penta, Primera Sangre)')
@app_commands.default_permissions(administrator=True)
async def bonus(interaction: discord.Interaction, usuario: discord.Member, puntos: int, motivo: str = ""):
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
                f'**{data["nombre"]}** ha recibido un bonus de **+{puntos} puntos**.\n'
                f'Motivo: {motivo}\nTotal bonus: +{data["bonus_total"]}')
            return
    await interaction.followup.send('Usuario no encontrado en el torneo.')


@tree.command(name='historial', description='(Admin) Ver historial de bonus y castigos')
@app_commands.describe(usuario='Jugador')
@app_commands.default_permissions(administrator=True)
async def historial(interaction: discord.Interaction, usuario: discord.Member):
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


@client.event
async def on_ready():
    print(f'Bot conectado como {client.user}')
    await tree.sync()
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
  .stats { display:flex; justify-content:center; gap:16px; margin-top:24px; flex-wrap:wrap; }
  .stat-card { background:#161b22; border-radius:10px; padding:14px 22px; min-width:120px; text-align:center; border:1px solid #2a2f3a; }
  .stat-card .num { font-size:1.6em; color:#f5c518; font-weight:bold; }
  .stat-card .label { font-size:0.75em; color:#9ca3af; text-transform:uppercase; letter-spacing:1px; }
  .contenedor { max-width:1000px; margin:30px auto; padding:0 20px; }
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
  footer { text-align:center; color:#6b7280; margin-top:40px; font-size:0.85em; }
</style>
</head>
<body>
<header>
  <h1>SoloQ Challenge</h1>
  <p>Torneo de ganancia de LP - {{ duracion }} dias</p>
  <div class="estado">{{ estado_torneo }}</div>
  <div class="stats">
    <div class="stat-card"><div class="num">{{ high|length + low|length }}</div><div class="label">Jugadores activos</div></div>
    <div class="stat-card"><div class="num">{{ pendientes|length }}</div><div class="label">Pendientes de revision</div></div>
    <div class="stat-card"><div class="num">{{ sin_voz|length }}</div><div class="label">Sin verificar voz</div></div>
  </div>
</header>
<div class="contenedor">
  <div class="aviso">Para que tus puntos sean validos debes conectarte al chat de voz del servidor de Discord (cualquier canal) mientras juegas tus partidas.</div>
  <div class="categoria">
    <h2>High Elo</h2>
    {% if high %}
    <table>
      <tr><th>#</th><th>Jugador</th><th>Rango actual</th><th>LP ganados</th><th>Bonus</th><th>Castigos</th><th>Voz</th><th>Escudos</th><th>Total</th></tr>
      {% for j in high %}
      <tr>
        <td class="{{ 'pos1' if loop.index==1 else ('pos2' if loop.index==2 else ('pos3' if loop.index==3 else '')) }}">{{ loop.index }}</td>
        <td>{{ j.nombre }}</td>
        <td>{{ j.tier_actual }} {{ j.rank_actual }}</td>
        <td>{{ j.lp_ganados }}</td>
        <td>+{{ j.bonus }}</td>
        <td>-{{ j.castigos }}</td>
        <td class="voz-ok">{{ j.tiempo_voz_min }} min</td>
        <td>{{ j.escudos }}</td>
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
      <tr><th>#</th><th>Jugador</th><th>Rango actual</th><th>LP ganados</th><th>Bonus</th><th>Castigos</th><th>Voz</th><th>Escudos</th><th>Total</th></tr>
      {% for j in low %}
      <tr>
        <td class="{{ 'pos1' if loop.index==1 else ('pos2' if loop.index==2 else ('pos3' if loop.index==3 else '')) }}">{{ loop.index }}</td>
        <td>{{ j.nombre }}</td>
        <td>{{ j.tier_actual }} {{ j.rank_actual }}</td>
        <td>{{ j.lp_ganados }}</td>
        <td>+{{ j.bonus }}</td>
        <td>-{{ j.castigos }}</td>
        <td class="voz-ok">{{ j.tiempo_voz_min }} min</td>
        <td>{{ j.escudos }}</td>
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
                                   duracion=DURACION_TORNEO, estado_torneo=calcular_estado_torneo(db))


@app.route('/api/tabla')
def api_tabla():
    db = cargar_db()
    high, low, pendientes, sin_voz = calcular_tabla(db)
    return jsonify({'high': high, 'low': low, 'pendientes': pendientes, 'sin_voz': sin_voz,
                     'estado_torneo': calcular_estado_torneo(db)})


Thread(target=lambda: app.run(host='0.0.0.0', port=10000), daemon=True).start()

client.run(DISCORD_TOKEN)
