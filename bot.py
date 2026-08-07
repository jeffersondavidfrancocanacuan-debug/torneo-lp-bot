import discord
from discord import app_commands
from discord.ext import tasks
import requests
import json
import os
import datetime
from threading import Thread
from flask import Flask

# ================= CONFIGURACIÓN =================
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
RIOT_API_KEY = os.environ.get('RIOT_API_KEY')
CANAL_CLASIFICACION_ID = int(os.environ.get('CANAL_CLASIFICACION_ID', '0'))
DURACION_TORNEO = int(os.environ.get('DURACION_TORNEO', '30'))
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


def obtener_info_ranked(riot_id, region):
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
                'puuid': puuid,
                'tier': entry['tier'],
                'rank': entry['rank'],
                'lp': entry['leaguePoints'],
                'wins': entry['wins'],
                'losses': entry['losses'],
                'nombre': nombre_completo
            }
    return {
        'puuid': puuid,
        'tier': 'UNRANKED',
        'rank': '',
        'lp': 0,
        'wins': 0,
        'losses': 0,
        'nombre': nombre_completo
    }


def determinar_elo(tier):
    high_tiers = ['MASTER', 'GRANDMASTER', 'CHALLENGER']
    return 'high' if tier.upper() in high_tiers else 'low'


@tree.command(name='registrar', description='Registra tu cuenta de LoL (LAN) para el torneo')
@app_commands.describe(nombre='Tu Riot ID completo, ej: Nombre#LAN1')
async def registrar(interaction: discord.Interaction, nombre: str):
    await interaction.response.defer()
    user_id = str(interaction.user.id)
    db = cargar_db()
    for jugador in db.values():
        if isinstance(jugador, dict) and jugador.get('discord_id') == user_id:
            await interaction.followup.send('Ya estás registrado.')
            return
    if '#' not in nombre:
        await interaction.followup.send('Usa tu Riot ID completo con formato Nombre#TAG (ej: Faker#LAN1).')
        return
    region = 'lan'
    info = obtener_info_ranked(nombre, region)
    if info is None:
        await interaction.followup.send('No se encontró la cuenta. Verifica el Riot ID exacto (Nombre#TAG) y que sea de LAN.')
        return
    ahora = datetime.datetime.now()
    db[info['puuid']] = {
        'discord_id': user_id,
        'nombre': info['nombre'],
        'region': region,
        'lp_inicial': info['lp'],
        'tier_inicial': info['tier'],
        'rank_inicial': info['rank'],
        'elo': determinar_elo(info['tier']),
        'fecha_registro': str(ahora),
        'bonus_total': 0,
        'castigos_total': 0
    }
    if 'inicio_torneo' not in db:
        db['inicio_torneo'] = str(ahora)
    guardar_db(db)
    await interaction.followup.send(
        f'{interaction.user.mention} registrado como {info["nombre"]} (LAN).\n'
        f'LP inicial: {info["lp"]} ({info["tier"]} {info["rank"]}).\n'
        f'Categoria: {"High Elo" if db[info["puuid"]]["elo"] == "high" else "Low Elo"}.\n'
        f'El torneo dura {DURACION_TORNEO} dias.'
    )


@tree.command(name='progreso', description='Mira tu progreso actual')
async def progreso(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = str(interaction.user.id)
    db = cargar_db()
    for puuid, data in db.items():
        if puuid == 'inicio_torneo' or not isinstance(data, dict):
            continue
        if data['discord_id'] == user_id:
            info = obtener_info_ranked(data['nombre'], data['region'])
            if info is None:
                await interaction.followup.send('No se pudo obtener tu informacion. Intenta mas tarde.')
                return
            lp_ganados = info['lp'] - data['lp_inicial']
            total = lp_ganados + data.get('bonus_total', 0) - data.get('castigos_total', 0)
            await interaction.followup.send(
                f'{data["nombre"]}\n'
                f'LP inicial: {data["lp_inicial"]} ({data["tier_inicial"]} {data["rank_inicial"]})\n'
                f'LP actual: {info["lp"]} ({info["tier"]} {info["rank"]})\n'
                f'LP ganados: {lp_ganados}\n'
                f'Bonus: +{data.get("bonus_total", 0)} | Castigos: -{data.get("castigos_total", 0)}\n'
                f'Total: {total} puntos'
            )
            return
    await interaction.followup.send('No estás registrado.')


@tree.command(name='tabla', description='Clasificación por categorías')
async def tabla(interaction: discord.Interaction):
    await interaction.response.defer()
    await mostrar_tabla(interaction.channel)
    await interaction.followup.send('Tabla actualizada.')


async def mostrar_tabla(canal):
    db = cargar_db()
    if not db or len([x for x in db.values() if isinstance(x, dict)]) == 0:
        await canal.send('No hay jugadores registrados.')
        return
    high, low = [], []
    for puuid, data in db.items():
        if puuid == 'inicio_torneo' or not isinstance(data, dict):
            continue
        info = obtener_info_ranked(data['nombre'], data['region'])
        if info is None:
            continue
        lp_ganados = info['lp'] - data['lp_inicial']
        total = lp_ganados + data.get('bonus_total', 0) - data.get('castigos_total', 0)
        jugador = {
            'discord_id': data['discord_id'],
            'nombre': data['nombre'],
            'lp_ganados': lp_ganados,
            'lp_actual': info['lp'],
            'tier_actual': info['tier'],
            'rank_actual': info['rank'],
            'elo': data.get('elo', 'low'),
            'bonus': data.get('bonus_total', 0),
            'castigos': data.get('castigos_total', 0),
            'total': total
        }
        (high if jugador['elo'] == 'high' else low).append(jugador)
    high.sort(key=lambda x: x['total'], reverse=True)
    low.sort(key=lambda x: x['total'], reverse=True)
    embed = discord.Embed(title='Clasificación del Torneo (30 días)', color=0x00ff00,
                          timestamp=datetime.datetime.now())
    if high:
        embed.add_field(name='High Elo (Master, GM, Challenger)', value='.', inline=False)
        for i, j in enumerate(high, 1):
            user = canal.guild.get_member(int(j['discord_id']))
            mention = user.mention if user else j['nombre']
            embed.add_field(
                name=f'{i}. {j["nombre"]}',
                value=f'{mention} -> {j["total"]} pts (LP: {j["lp_ganados"]}, Bonus: +{j["bonus"]}, Castigos: -{j["castigos"]})',
                inline=False
            )
    if low:
        embed.add_field(name='Low Elo (Hierro - Diamante)', value='.', inline=False)
        for i, j in enumerate(low, 1):
            user = canal.guild.get_member(int(j['discord_id']))
            mention = user.mention if user else j['nombre']
            embed.add_field(
                name=f'{i}. {j["nombre"]}',
                value=f'{mention} -> {j["total"]} pts (LP: {j["lp_ganados"]}, Bonus: +{j["bonus"]}, Castigos: -{j["castigos"]})',
                inline=False
            )
    embed.set_footer(text=f'Torneo de {DURACION_TORNEO} días - Se actualiza cada 30 min')
    await canal.send(embed=embed)


@tree.command(name='castigar', description='(Admin) Resta puntos a un jugador')
@app_commands.describe(usuario='Jugador a castigar', puntos='Puntos a restar', motivo='Razón del castigo')
@app_commands.default_permissions(administrator=True)
async def castigar(interaction: discord.Interaction, usuario: discord.Member, puntos: int, motivo: str = ""):
    await interaction.response.defer()
    if puntos <= 0:
        await interaction.followup.send('Los puntos deben ser positivos.')
        return
    db = cargar_db()
    for puuid, data in db.items():
        if puuid == 'inicio_torneo' or not isinstance(data, dict):
            continue
        if data['discord_id'] == str(usuario.id):
            data['castigos_total'] = data.get('castigos_total', 0) + puntos
            guardar_db(db)
            registros = cargar_registros()
            registros.append({
                'tipo': 'castigo',
                'usuario': str(usuario.id),
                'nombre': data['nombre'],
                'puntos': puntos,
                'motivo': motivo,
                'fecha': str(datetime.datetime.now()),
                'admin': str(interaction.user.id)
            })
            guardar_registros(registros)
            await interaction.followup.send(
                f'{data["nombre"]} ha recibido un castigo de -{puntos} puntos.\n'
                f'Motivo: {motivo}\nTotal castigos: -{data["castigos_total"]}')
            return
    await interaction.followup.send('Usuario no encontrado en el torneo.')


@tree.command(name='bonus', description='(Admin) Otorga puntos extra a un jugador')
@app_commands.describe(usuario='Jugador a bonificar', puntos='Puntos a sumar', motivo='Razón (ej. Penta, Primera Sangre)')
@app_commands.default_permissions(administrator=True)
async def bonus(interaction: discord.Interaction, usuario: discord.Member, puntos: int, motivo: str = ""):
    await interaction.response.defer()
    if puntos <= 0:
        await interaction.followup.send('Los puntos deben ser positivos.')
        return
    db = cargar_db()
    for puuid, data in db.items():
        if puuid == 'inicio_torneo' or not isinstance(data, dict):
            continue
        if data['discord_id'] == str(usuario.id):
            data['bonus_total'] = data.get('bonus_total', 0) + puntos
            guardar_db(db)
            registros = cargar_registros()
            registros.append({
                'tipo': 'bonus',
                'usuario': str(usuario.id),
                'nombre': data['nombre'],
                'puntos': puntos,
                'motivo': motivo,
                'fecha': str(datetime.datetime.now()),
                'admin': str(interaction.user.id)
            })
            guardar_registros(registros)
            await interaction.followup.send(
                f'{data["nombre"]} ha recibido un bonus de +{puntos} puntos.\n'
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
        mensaje += f'{simbolo} {r["puntos"]} pts - {r["motivo"]} ({r["fecha"][:10]})\n'
    await interaction.followup.send(mensaje)


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


app = Flask(__name__)


@app.route('/')
def home():
    return "Bot del torneo funcionando"


Thread(target=lambda: app.run(host='0.0.0.0', port=10000), daemon=True).start()

client.run(DISCORD_TOKEN)
