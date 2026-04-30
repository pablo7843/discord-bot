import os
import discord
from discord import app_commands
from discord.ext import tasks
from supabase import create_client, Client
from dotenv import load_dotenv
import datetime
import pytz
import dateparser
# Importaciones nuevas para Render
from flask import Flask
from threading import Thread

# --- FUNCIONALIDAD KEEP ALIVE PARA RENDER ---
app = Flask('')

@app.route('/')
def home():
    return "Bot está vivo!"

def run():
    # Render usa puertos dinámicos, el 8080 es el estándar de la industria
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()
# --------------------------------------------

# 1. Configuración Inicial
load_dotenv()
ZONA_ES = pytz.timezone('Europe/Madrid')

class CalendarBot(discord.Client):
    def __init__(self):
        # Intents básicos: el bot solo necesita responder a comandos
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.supabase: Client = create_client(
            os.getenv("SUPABASE_URL"), 
            os.getenv("SUPABASE_KEY")
        )

    async def setup_hook(self):
        # Inicia el bucle que revisa la base de datos cada minuto
        self.revisar_alertas.start()

    async def on_ready(self):
        print(f'✅ Bot iniciado como {self.user}')
        try:
            print("Sincronizando comandos con Discord...")
            await self.tree.sync()
            print("🚀 Comandos sincronizados correctamente.")
        except Exception as e:
            print(f"⚠️ Error al sincronizar comandos: {e}")

    # --- SISTEMA DE NOTIFICACIONES (Segundo Plano) ---
    @tasks.loop(minutes=1)
    async def revisar_alertas(self):
        ahora_utc = datetime.datetime.now(pytz.utc)
        try:
            # Buscar tareas pendientes que ya deberían haberse ejecutado
            res = self.supabase.table("recordatorios")\
                .select("*")\
                .eq("completado", False)\
                .lte("fecha_ejecucion", ahora_utc.isoformat())\
                .execute()

            for r in res.data:
                user = await self.fetch_user(r['usuario_id'])
                if user:
                    embed = discord.Embed(
                        title="⏰ ¡RECORDATORIO!",
                        description=f"**Tarea:** {r['tarea']}\n{r.get('descripcion', '')}",
                        color=discord.Color.red()
                    )
                    try:
                        await user.send(embed=embed)
                        # Marcar como hecha para no repetir el aviso
                        self.supabase.table("recordatorios").update({"completado": True}).eq("id", r['id']).execute()
                    except Exception as e:
                        print(f"No pude enviar DM al usuario {r['usuario_id']}: {e}")
        except Exception as e:
            print(f"Error en el bucle de revisión: {e}")

# Instanciamos el bot
client = CalendarBot()

# --- COMANDOS SLASH (CRUD) ---

@client.tree.command(name="nuevo", description="Añade un recordatorio al calendario")
@app_commands.describe(tarea="¿Qué hay que hacer?", cuando="Ej: mañana a las 10:00, el viernes a las 3pm", descripcion="Detalles extra")
async def nuevo(interaction: discord.Interaction, tarea: str, cuando: str, descripcion: str = ""):
    # Interpretamos la fecha relativa a España y la pasamos a UTC para la DB
    fecha_dt = dateparser.parse(cuando, settings={
        'PREFER_DATES_FROM': 'future', 
        'TIMEZONE': 'Europe/Madrid', 
        'TO_TIMEZONE': 'UTC', 
        'RETURN_AS_TIMEZONE_AWARE': True
    })

    if not fecha_dt:
        return await interaction.response.send_message("❌ No entendí la fecha. Intenta algo como 'mañana a las 14:00'", ephemeral=True)

    data = {
        "usuario_id": interaction.user.id,
        "tarea": tarea,
        "descripcion": descripcion,
        "fecha_ejecucion": fecha_dt.isoformat(),
        "completado": False
    }
    
    try:
        client.supabase.table("recordatorios").insert(data).execute()
        
        fecha_local = fecha_dt.astimezone(ZONA_ES).strftime('%d/%m/%Y a las %H:%M')
        embed = discord.Embed(title="✅ Recordatorio Guardado", color=discord.Color.green())
        embed.add_field(name="Tarea", value=tarea)
        embed.add_field(name="Fecha (España)", value=fecha_local)
        if descripcion:
            embed.add_field(name="Descripción", value=descripcion, inline=False)
            
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error al guardar en Supabase: {e}", ephemeral=True)

@client.tree.command(name="listar", description="Muestra todos tus recordatorios pendientes")
async def listar(interaction: discord.Interaction):
    try:
        res = client.supabase.table("recordatorios")\
            .select("*")\
            .eq("usuario_id", interaction.user.id)\
            .eq("completado", False)\
            .order("fecha_ejecucion")\
            .execute()

        if not res.data:
            return await interaction.response.send_message("No tienes recordatorios pendientes.", ephemeral=True)

        embed = discord.Embed(title="📅 Tus Recordatorios", color=discord.Color.blue())
        for r in res.data:
            fecha_local = datetime.datetime.fromisoformat(r['fecha_ejecucion']).astimezone(ZONA_ES)
            embed.add_field(
                name=f"🆔 {r['id']} | {r['tarea']}",
                value=f"🕒 {fecha_local.strftime('%d/%m %H:%M')}",
                inline=False
            )
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"Error al listar: {e}", ephemeral=True)

@client.tree.command(name="eliminar", description="Elimina un recordatorio usando su ID")
async def eliminar(interaction: discord.Interaction, id_tarea: int):
    try:
        # Primero verificamos si la tarea existe y es del usuario
        check = client.supabase.table("recordatorios").select("usuario_id").eq("id", id_tarea).execute()
        
        if not check.data or check.data[0]['usuario_id'] != interaction.user.id:
            return await interaction.response.send_message("❌ No se encontró la tarea o no tienes permiso.", ephemeral=True)

        client.supabase.table("recordatorios").delete().eq("id", id_tarea).execute()
        await interaction.response.send_message(f"🗑️ Recordatorio `{id_tarea}` eliminado correctamente.")
    except Exception as e:
        await interaction.response.send_message(f"Error al eliminar: {e}", ephemeral=True)

@client.tree.command(name="hoy", description="Muestra las tareas que tienes para hoy")
async def hoy(interaction: discord.Interaction):
    # Rango de hoy (00:00 a 23:59) en hora de España
    ahora_es = datetime.datetime.now(ZONA_ES)
    inicio_dia = ahora_es.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(pytz.utc)
    fin_dia = inicio_dia + datetime.timedelta(days=1)

    try:
        res = client.supabase.table("recordatorios")\
            .select("*")\
            .eq("usuario_id", interaction.user.id)\
            .gte("fecha_ejecucion", inicio_dia.isoformat())\
            .lt("fecha_ejecucion", fin_dia.isoformat())\
            .execute()

        embed = discord.Embed(title=f"📌 Agenda para hoy ({ahora_es.strftime('%d/%m')})", color=discord.Color.orange())
        
        if not res.data:
            embed.description = "No tienes nada programado para hoy."
        else:
            for r in res.data:
                hora = datetime.datetime.fromisoformat(r['fecha_ejecucion']).astimezone(ZONA_ES).strftime('%H:%M')
                estado = "✅" if r['completado'] else "⏳"
                embed.add_field(name=f"{estado} {hora} - {r['tarea']}", value=r.get('descripcion', 'Sin detalles'), inline=False)
        
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"Error al obtener agenda: {e}", ephemeral=True)

# --- EJECUCIÓN ---
if __name__ == "__main__":
    # Arrancamos el servidor web Flask en segundo plano
    keep_alive()
    # Ejecución del bot de Discord
    client.run(os.getenv("DISCORD_TOKEN"))