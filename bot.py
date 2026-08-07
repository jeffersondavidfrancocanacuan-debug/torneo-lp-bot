import discord
from discord import app_commands
from discord.ext import tasks
import requests
import json
import os
import datetime
from threading import Thread
from flask import Flask, jsonify, render_template_string

# ================= CONFIGURACIÓN =================
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
RIOT_API_KEY = os.environ.get('RIOT_API_KEY')
CANAL_CLASIFICACION_ID = int(os.environ.get('CANAL_CLASIFICACION_ID', '0'))
DURACION_TORNEO = int(os.environ.get('DURACION_TORNEO', '30'))
JUEGOS_MINIMOS_CUENTA = int(os.environ.get('JUEGOS_MINIMOS_CUENTA', '15'))
# =================================================

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

DB_FILE = 'jugadores.json'
REGISTROS_FILE = 'castigos_bonus.json'

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


# ------------------- PERSISTENCIA -------------------

def cargar_db():
    try:
        with open(DB_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_db(db):
    with open(DB_FILE, 'w') as f:
        json.dump(db, f, indent=4)


def cargar_registros():
    try:
        with open(REGISTROS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return []


def guardar_registros(registros):
    with open(REGISTROS_FILE, 'w') as f:
        json.dump(registros, f, indent=4)


def jugadores_validos(db):
    return {k: v for k, v in db.items() if k != 'inicio_torneo' and isinstance(v, dict)}



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



# ------------------- CALCULO DE TABLA -------------------

def calcular_tabla(db):
    """Devuelve (high, low, pendientes) con los datos ya frescos de Riot."""
    high, low, pendientes = [], [], []
    for puuid, data in jugadores_validos(db).items():
        info = obtener_info_ranked(data['nombre'], data['region'])
        if info is None:
            continue
        lp_ganados = info['lp'] - data['lp_inicial']
        total = lp_ganados + data.get('bonus_total', 0) - data.get('castigos_total', 0)
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
        }
        if jugador['estado'] == 'pendiente':
            pendientes.append(jugador)
        elif jugador['elo'] == 'high':
            high.append(jugador)
        else:
            low.append(jugador)
    high.sort(key=lambda x: x['total'], reverse=True)
    low.sort(key=lambda x: x['total'], reverse=True)
    return high, low, pendientes


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
                for clave in recien_desbloqueados:
                    info_logro = LOGROS.get(clave)
                    if info_logro:
                        anuncios.append(f"{info_logro['nombre']} - **{j['nombre']}** {info_logro['desc']} ({'High' if categoria == 'high' else 'Low'} Elo)")

    guardar_db(db)

    if anuncios and canal:
        try:
            texto = '**Nuevos logros desbloqueados**\n' + '\n'.join(anuncios[:10])
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
@app_commands.describe(nombre='Tu Riot ID completo, ej: Nombre#LAN1')
async def registrar(interaction: discord.Interaction, nombre: str):
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
    }
    if 'inicio_torneo' not in db:
        db['inicio_torneo'] = str(ahora)
    guardar_db(db)

    categoria_txt = "High Elo" if db[info["puuid"]]["elo"] == "high" else "Low Elo"
    if estado == 'pendiente':
        await interaction.followup.send(
            f'{interaction.user.mention} registrado como **{info["nombre"]}** (LAN).\n'
            f'Tu cuenta tiene solo {total_partidas} partidas en soloQ, por lo que queda **pendiente de revision** '
            f'por la directiva antes de aparecer en la tabla (posible cuenta nueva/comprada).\n'
            f'Categoria sugerida: {categoria_txt}.'
        )
    else:
        await interaction.followup.send(
            f'{interaction.user.mention} registrado como **{info["nombre"]}** (LAN).\n'
            f'LP inicial: {info["lp"]} ({info["tier"]} {info["rank"]}).\n'
            f'Categoria: {categoria_txt}.\n'
            f'A jugar! El torneo dura {DURACION_TORNEO} dias.'
        )


@tree.command(name='progreso', description='Mira tu progreso actual')
async def progreso(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = str(interaction.user.id)
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
            await interaction.followup.send(
                f'**{data["nombre"]}** ({estado_txt})\n'
                f'LP inicial: {data["lp_inicial"]} ({data["tier_inicial"]} {data["rank_inicial"]})\n'
                f'LP actual: {info["lp"]} ({info["tier"]} {info["rank"]})\n'
                f'LP ganados: {lp_ganados}\n'
                f'Bonus: +{data.get("bonus_total", 0)} | Castigos: -{data.get("castigos_total", 0)}\n'
                f'**Total: {total} puntos**'
            )
            return
    await interaction.followup.send('No estas registrado.')


@tree.command(name='perfil', description='Muestra tu tarjeta de jugador completa')
async def perfil(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = str(interaction.user.id)
    db = cargar_db()
    high, low, pendientes = calcular_tabla(db)

    objetivo = None
    for j in high + low + pendientes:
        if j['discord_id'] == user_id:
            objetivo = j
            break
    if objetivo is None:
        await interaction.followup.send('No estas registrado. Usa `/registrar` primero.')
        return

    if objetivo['estado'] == 'pendiente':
        posicion_txt = 'En revision (no aparece en la tabla aun)'
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
    embed.add_field(name='Total', value=f'**{objetivo["total"]} pts**', inline=True)
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
    high, low, pendientes = calcular_tabla(db)

    embed = discord.Embed(title='Clasificacion del Torneo (30 dias)', color=0x00ff00,
                          timestamp=datetime.datetime.now())
    if high:
        embed.add_field(name='High Elo (Master, GM, Challenger)', value='​', inline=False)
        for i, j in enumerate(high, 1):
            user = canal.guild.get_member(int(j['discord_id']))
            mention = user.mention if user else j['nombre']
            embed.add_field(
                name=f'{i}. {j["nombre"]}',
                value=f'{mention} -> {j["total"]} pts (LP: {j["lp_ganados"]}, Bonus: +{j["bonus"]}, Castigos: -{j["castigos"]})',
                inline=False
            )
    if low:
        embed.add_field(name='Low Elo (Hierro - Diamante)', value='​', inline=False)
        for i, j in enumerate(low, 1):
            user = canal.guild.get_member(int(j['discord_id']))
            mention = user.mention if user else j['nombre']
            embed.add_field(
                name=f'{i}. {j["nombre"]}',
                value=f'{mention} -> {j["total"]} pts (LP: {j["lp_ganados"]}, Bonus: +{j["bonus"]}, Castigos: -{j["castigos"]})',
                inline=False
            )
    if pendientes:
        embed.add_field(name='Pendientes de revision', value='​', inline=False)
        for j in pendientes:
            embed.add_field(name=j['nombre'], value='Esperando aprobacion de la directiva (`/clasificar`)', inline=False)
    embed.set_footer(text=f'Torneo de {DURACION_TORNEO} dias - Se actualiza cada 30 min - Web: ver enlace fijado')
    await canal.send(embed=embed)
    await procesar_logros_y_roles(canal, high, low, db)


@tree.command(name='ayuda', description='Muestra todos los comandos disponibles')
async def ayuda(interaction: discord.Interaction):
    embed = discord.Embed(title='SoloQ Challenge - Comandos', color=0x5865F2,
                           description='Torneo de ganancia de LP de 30 dias.')
    embed.add_field(
        name='Jugadores',
        value=('`/registrar` - Inscribete con tu Riot ID (Nombre#TAG)\n'
               '`/progreso` - Consulta tu propio avance de LP\n'
               '`/perfil` - Tu tarjeta completa (posicion, logros, etc.)\n'
               '`/tabla` - Muestra la clasificacion al instante'),
        inline=False
    )
    embed.add_field(
        name='Administracion',
        value=('`/bonus` - Otorga puntos extra a un jugador\n'
               '`/castigar` - Aplica una penalizacion\n'
               '`/historial` - Revisa el historial de cambios de un jugador\n'
               '`/pendientes` - Lista cuentas nuevas esperando revision\n'
               '`/clasificar` - Aprueba y asigna categoria a una cuenta pendiente'),
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
    _, _, pend = calcular_tabla(db)
    if not pend:
        await interaction.followup.send('No hay cuentas pendientes de revision.')
        return
    mensaje = '**Cuentas pendientes:**\n'
    for j in pend:
        mensaje += f'- **{j["nombre"]}** - {j["tier_actual"]} {j["rank_actual"]} - <@{j["discord_id"]}>\n'
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
                f'**{data["nombre"]}** aprobado y clasificado en **{"High" if categoria.value == "high" else "Low"} Elo**. Ya aparece en la tabla.')
            return
    await interaction.followup.send('Usuario no encontrado en el torneo.')


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


# ------------------- TAREA AUTOMATICA -------------------

@tasks.loop(minutes=30)
async def actualizar_canal():
    if CANAL_CLASIFICACION_ID == 0:
        return
    canal = client.get_channel(CANAL_CLASIFICACION_ID)
    if canal is None:
        return
    await canal.purge(limit=5)
    await mostrar_tabla(canal)


@client.event
async def on_ready():
    print(f'Bot conectado como {client.user}')
    await tree.sync()
    if not actualizar_canal.is_running():
        actualizar_canal.start()



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
  body { background:#0f1117; color:#e8e8e8; font-family: 'Segoe UI', Arial, sans-serif; margin:0; padding:0 0 60px; }
  header { background:linear-gradient(135deg,#1f2937,#111827); padding:40px 20px; text-align:center; border-bottom:3px solid #f5c518; }
  header h1 { margin:0; font-size:2.4em; color:#f5c518; letter-spacing:1px; }
  header p { color:#9ca3af; margin-top:8px; }
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
  .vacio { color:#6b7280; padding:20px; text-align:center; }
  footer { text-align:center; color:#6b7280; margin-top:40px; font-size:0.85em; }
</style>
</head>
<body>
<header>
  <h1>SoloQ Challenge</h1>
  <p>Torneo de ganancia de LP - {{ duracion }} dias - Se actualiza automaticamente</p>
</header>
<div class="contenedor">
  <div class="categoria">
    <h2>High Elo</h2>
    {% if high %}
    <table>
      <tr><th>#</th><th>Jugador</th><th>Rango actual</th><th>LP ganados</th><th>Bonus</th><th>Castigos</th><th>Total</th></tr>
      {% for j in high %}
      <tr>
        <td class="{{ 'pos1' if loop.index==1 else ('pos2' if loop.index==2 else ('pos3' if loop.index==3 else '')) }}">{{ loop.index }}</td>
        <td>{{ j.nombre }}</td>
        <td>{{ j.tier_actual }} {{ j.rank_actual }}</td>
        <td>{{ j.lp_ganados }}</td>
        <td>+{{ j.bonus }}</td>
        <td>-{{ j.castigos }}</td>
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
      <tr><th>#</th><th>Jugador</th><th>Rango actual</th><th>LP ganados</th><th>Bonus</th><th>Castigos</th><th>Total</th></tr>
      {% for j in low %}
      <tr>
        <td class="{{ 'pos1' if loop.index==1 else ('pos2' if loop.index==2 else ('pos3' if loop.index==3 else '')) }}">{{ loop.index }}</td>
        <td>{{ j.nombre }}</td>
        <td>{{ j.tier_actual }} {{ j.rank_actual }}</td>
        <td>{{ j.lp_ganados }}</td>
        <td>+{{ j.bonus }}</td>
        <td>-{{ j.castigos }}</td>
        <td><b>{{ j.total }}</b></td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <p class="vacio">Aun no hay jugadores en esta categoria.</p>
    {% endif %}
  </div>
</div>
<footer>Actualizado automaticamente - Pagina se refresca cada 60 segundos</footer>
</body>
</html>
"""


@app.route('/')
def home():
    db = cargar_db()
    high, low, _ = calcular_tabla(db)
    return render_template_string(PAGINA_HTML, high=high, low=low, duracion=DURACION_TORNEO)


@app.route('/api/tabla')
def api_tabla():
    db = cargar_db()
    high, low, pendientes = calcular_tabla(db)
    return jsonify({'high': high, 'low': low, 'pendientes': pendientes})


Thread(target=lambda: app.run(host='0.0.0.0', port=10000), daemon=True).start()

client.run(DISCORD_TOKEN)
