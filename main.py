import os
import discord
from discord import app_commands
from discord.ext import tasks
from supabase import create_client, Client
from dotenv import load_dotenv
import datetime
import pytz
import dateparser
from flask import Flask
from threading import Thread

# ── Keep-alive para Render ────────────────────────────────────────────────────
app = Flask('')

@app.route('/')
def home():
    return "Bot está vivo!"

def keep_alive():
    t = Thread(target=lambda: app.run(host='0.0.0.0', port=8080))
    t.daemon = True
    t.start()

# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()
ZONA_ES = pytz.timezone('Europe/Madrid')
DIAS_ES = {
    0: "Lunes", 1: "Martes", 2: "Miércoles",
    3: "Jueves", 4: "Viernes", 5: "Sábado", 6: "Domingo"
}


def _parsear_fecha(cuando: str) -> datetime.datetime | None:
    return dateparser.parse(cuando, settings={
        'PREFER_DATES_FROM': 'future',
        'TIMEZONE': 'Europe/Madrid',
        'TO_TIMEZONE': 'UTC',
        'RETURN_AS_TIMEZONE_AWARE': True
    })


def _dt_de_str(s: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=pytz.utc)


class CalendarBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.supabase: Client = create_client(
            os.getenv("SUPABASE_URL"),
            os.getenv("SUPABASE_KEY")
        )

    async def setup_hook(self):
        self.revisar_alertas.start()

    async def on_ready(self):
        print(f'✅ Bot iniciado como {self.user}')
        await self.tree.sync()
        print("🚀 Comandos sincronizados.")

    @tasks.loop(minutes=1)
    async def revisar_alertas(self):
        ahora_utc = datetime.datetime.now(pytz.utc)
        try:
            # Recordatorios que ya tocaron
            res = self.supabase.table("recordatorios")\
                .select("*")\
                .eq("completado", False)\
                .lte("fecha_ejecucion", ahora_utc.isoformat())\
                .execute()

            for r in res.data:
                embed = discord.Embed(
                    title="⏰ ¡RECORDATORIO!",
                    description=f"**{r['tarea']}**",
                    color=discord.Color.red()
                )
                if r.get('descripcion'):
                    embed.add_field(name="Detalles", value=r['descripcion'], inline=False)
                try:
                    user = await self.fetch_user(int(r['usuario_id']))
                    if user:
                        await user.send(embed=embed)
                except Exception as e:
                    print(f"Error enviando recordatorio {r['id']}: {e}")
                self.supabase.table("recordatorios")\
                    .update({"completado": True})\
                    .eq("id", r['id'])\
                    .execute()

            # Pre-avisos: ventana 25-35 min antes
            ventana_ini = (ahora_utc + datetime.timedelta(minutes=25)).isoformat()
            ventana_fin = (ahora_utc + datetime.timedelta(minutes=35)).isoformat()

            preaviso = self.supabase.table("recordatorios")\
                .select("*")\
                .eq("completado", False)\
                .eq("preaviso_enviado", False)\
                .gte("fecha_ejecucion", ventana_ini)\
                .lte("fecha_ejecucion", ventana_fin)\
                .execute()

            for r in preaviso.data:
                try:
                    user = await self.fetch_user(int(r['usuario_id']))
                    if user:
                        fecha_local = _dt_de_str(r['fecha_ejecucion']).astimezone(ZONA_ES)
                        embed = discord.Embed(
                            title="🔔 Recordatorio en ~30 minutos",
                            description=f"**{r['tarea']}**\n🕒 A las {fecha_local.strftime('%H:%M')}",
                            color=discord.Color.yellow()
                        )
                        await user.send(embed=embed)
                except Exception as e:
                    print(f"Error enviando pre-aviso {r['id']}: {e}")
                self.supabase.table("recordatorios")\
                    .update({"preaviso_enviado": True})\
                    .eq("id", r['id'])\
                    .execute()

        except Exception as e:
            print(f"Error en el bucle de revisión: {e}")

    @revisar_alertas.before_loop
    async def before_revisar_alertas(self):
        await self.wait_until_ready()


client = CalendarBot()


# ── /nuevo ────────────────────────────────────────────────────────────────────
@client.tree.command(name="nuevo", description="Añade un recordatorio")
@app_commands.describe(
    tarea="¿Qué hay que hacer?",
    cuando="Ej: mañana a las 10:00, el viernes a las 3pm, en 2 horas",
    descripcion="Detalles extra (opcional)"
)
async def nuevo(interaction: discord.Interaction, tarea: str, cuando: str, descripcion: str = ""):
    fecha_dt = _parsear_fecha(cuando)
    if not fecha_dt:
        return await interaction.response.send_message(
            "❌ No entendí la fecha. Prueba: 'mañana a las 14:00', 'el lunes a las 9', 'en 2 horas'.",
            ephemeral=True
        )
    try:
        client.supabase.table("recordatorios").insert({
            "usuario_id": interaction.user.id,
            "tarea": tarea,
            "descripcion": descripcion,
            "fecha_ejecucion": fecha_dt.isoformat(),
            "completado": False,
            "preaviso_enviado": False
        }).execute()

        fecha_local = fecha_dt.astimezone(ZONA_ES).strftime('%d/%m/%Y a las %H:%M')
        embed = discord.Embed(title="✅ Recordatorio guardado", color=discord.Color.green())
        embed.add_field(name="Tarea", value=tarea, inline=False)
        embed.add_field(name="Cuándo (España)", value=fecha_local, inline=False)
        if descripcion:
            embed.add_field(name="Descripción", value=descripcion, inline=False)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error al guardar: {e}", ephemeral=True)


# ── /listar ───────────────────────────────────────────────────────────────────
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
            return await interaction.response.send_message(
                "No tienes recordatorios pendientes.", ephemeral=True
            )

        embed = discord.Embed(title="📅 Tus recordatorios pendientes", color=discord.Color.blue())
        for r in res.data:
            fecha_local = _dt_de_str(r['fecha_ejecucion']).astimezone(ZONA_ES)
            embed.add_field(
                name=f"🆔 {r['id']} — {r['tarea']}",
                value=f"🕒 {fecha_local.strftime('%d/%m/%Y a las %H:%M')}",
                inline=False
            )
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


# ── /hoy ──────────────────────────────────────────────────────────────────────
@client.tree.command(name="hoy", description="Tareas programadas para hoy")
async def hoy(interaction: discord.Interaction):
    ahora_es = datetime.datetime.now(ZONA_ES)
    inicio = ahora_es.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(pytz.utc)
    fin = inicio + datetime.timedelta(days=1)
    try:
        res = client.supabase.table("recordatorios")\
            .select("*")\
            .eq("usuario_id", interaction.user.id)\
            .gte("fecha_ejecucion", inicio.isoformat())\
            .lt("fecha_ejecucion", fin.isoformat())\
            .order("fecha_ejecucion")\
            .execute()

        embed = discord.Embed(
            title=f"📌 {DIAS_ES[ahora_es.weekday()]} {ahora_es.strftime('%d/%m/%Y')}",
            color=discord.Color.orange()
        )
        if not res.data:
            embed.description = "No tienes nada programado para hoy. 🎉"
        else:
            for r in res.data:
                hora = _dt_de_str(r['fecha_ejecucion']).astimezone(ZONA_ES).strftime('%H:%M')
                estado = "✅" if r['completado'] else "⏳"
                embed.add_field(
                    name=f"{estado} {hora} — {r['tarea']}",
                    value=r.get('descripcion') or "Sin detalles",
                    inline=False
                )
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


# ── /semana ───────────────────────────────────────────────────────────────────
@client.tree.command(name="semana", description="Agenda de los próximos 7 días")
async def semana(interaction: discord.Interaction):
    ahora_es = datetime.datetime.now(ZONA_ES)
    inicio = ahora_es.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(pytz.utc)
    fin = inicio + datetime.timedelta(days=7)
    try:
        res = client.supabase.table("recordatorios")\
            .select("*")\
            .eq("usuario_id", interaction.user.id)\
            .eq("completado", False)\
            .gte("fecha_ejecucion", inicio.isoformat())\
            .lt("fecha_ejecucion", fin.isoformat())\
            .order("fecha_ejecucion")\
            .execute()

        embed = discord.Embed(title="📆 Agenda — próximos 7 días", color=discord.Color.purple())
        if not res.data:
            embed.description = "No tienes nada para esta semana. ✨"
        else:
            dia_actual = None
            for r in res.data:
                fecha_local = _dt_de_str(r['fecha_ejecucion']).astimezone(ZONA_ES)
                dia_str = f"{DIAS_ES[fecha_local.weekday()]} {fecha_local.strftime('%d/%m')}"
                if dia_str != dia_actual:
                    dia_actual = dia_str
                    embed.add_field(name=f"── {dia_str} ──", value="​", inline=False)
                embed.add_field(
                    name=f"🆔{r['id']} {fecha_local.strftime('%H:%M')} — {r['tarea']}",
                    value=r.get('descripcion') or "Sin detalles",
                    inline=False
                )
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


# ── /eliminar ─────────────────────────────────────────────────────────────────
@client.tree.command(name="eliminar", description="Elimina un recordatorio por su ID")
@app_commands.describe(id_tarea="ID del recordatorio (usa /listar para verlos)")
async def eliminar(interaction: discord.Interaction, id_tarea: int):
    try:
        check = client.supabase.table("recordatorios")\
            .select("usuario_id").eq("id", id_tarea).execute()

        if not check.data or int(check.data[0]['usuario_id']) != interaction.user.id:
            return await interaction.response.send_message(
                "❌ No encontrado o sin permiso.", ephemeral=True
            )
        client.supabase.table("recordatorios").delete().eq("id", id_tarea).execute()
        await interaction.response.send_message(f"🗑️ Recordatorio `{id_tarea}` eliminado.")
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


# ── /editar ───────────────────────────────────────────────────────────────────
@client.tree.command(name="editar", description="Cambia la fecha o descripción de un recordatorio")
@app_commands.describe(
    id_tarea="ID del recordatorio",
    cuando="Nueva fecha/hora (opcional): 'el lunes a las 10', 'en 3 horas'",
    descripcion="Nueva descripción (opcional)"
)
async def editar(interaction: discord.Interaction, id_tarea: int, cuando: str = "", descripcion: str = ""):
    try:
        check = client.supabase.table("recordatorios")\
            .select("usuario_id").eq("id", id_tarea).execute()

        if not check.data or int(check.data[0]['usuario_id']) != interaction.user.id:
            return await interaction.response.send_message(
                "❌ No encontrado o sin permiso.", ephemeral=True
            )
        if not cuando and not descripcion:
            return await interaction.response.send_message(
                "Indica al menos una cosa a cambiar: `cuando` o `descripcion`.", ephemeral=True
            )

        nueva_fecha = None
        cambios = {}
        if cuando:
            nueva_fecha = _parsear_fecha(cuando)
            if not nueva_fecha:
                return await interaction.response.send_message(
                    "❌ No entendí la nueva fecha.", ephemeral=True
                )
            cambios["fecha_ejecucion"] = nueva_fecha.isoformat()
            cambios["preaviso_enviado"] = False
        if descripcion:
            cambios["descripcion"] = descripcion

        client.supabase.table("recordatorios").update(cambios).eq("id", id_tarea).execute()

        embed = discord.Embed(title="✏️ Recordatorio actualizado", color=discord.Color.green())
        embed.add_field(name="ID", value=str(id_tarea), inline=True)
        if nueva_fecha:
            embed.add_field(
                name="Nueva fecha",
                value=nueva_fecha.astimezone(ZONA_ES).strftime('%d/%m/%Y a las %H:%M'),
                inline=True
            )
        if descripcion:
            embed.add_field(name="Nueva descripción", value=descripcion, inline=False)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


# ── /posponer ─────────────────────────────────────────────────────────────────
@client.tree.command(name="posponer", description="Pospone un recordatorio a una nueva fecha/hora")
@app_commands.describe(
    id_tarea="ID del recordatorio",
    cuando="Nueva fecha: 'en 1 hora', 'en 30 minutos', 'mañana a las 10'"
)
async def posponer(interaction: discord.Interaction, id_tarea: int, cuando: str):
    try:
        check = client.supabase.table("recordatorios")\
            .select("usuario_id").eq("id", id_tarea).execute()

        if not check.data or int(check.data[0]['usuario_id']) != interaction.user.id:
            return await interaction.response.send_message(
                "❌ No encontrado o sin permiso.", ephemeral=True
            )

        nueva_fecha = _parsear_fecha(cuando)
        if not nueva_fecha:
            return await interaction.response.send_message(
                "❌ No entendí. Prueba: 'en 1 hora', 'en 30 minutos', 'mañana a las 10'.", ephemeral=True
            )

        client.supabase.table("recordatorios").update({
            "fecha_ejecucion": nueva_fecha.isoformat(),
            "completado": False,
            "preaviso_enviado": False
        }).eq("id", id_tarea).execute()

        fecha_local = nueva_fecha.astimezone(ZONA_ES).strftime('%d/%m/%Y a las %H:%M')
        await interaction.response.send_message(
            f"⏩ Recordatorio `{id_tarea}` pospuesto hasta el **{fecha_local}**."
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


# ── /completar ────────────────────────────────────────────────────────────────
@client.tree.command(name="completar", description="Marca un recordatorio como completado manualmente")
@app_commands.describe(id_tarea="ID del recordatorio")
async def completar(interaction: discord.Interaction, id_tarea: int):
    try:
        check = client.supabase.table("recordatorios")\
            .select("usuario_id").eq("id", id_tarea).execute()

        if not check.data or int(check.data[0]['usuario_id']) != interaction.user.id:
            return await interaction.response.send_message(
                "❌ No encontrado o sin permiso.", ephemeral=True
            )
        client.supabase.table("recordatorios")\
            .update({"completado": True}).eq("id", id_tarea).execute()
        await interaction.response.send_message(f"✅ Recordatorio `{id_tarea}` marcado como completado.")
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


# ── /buscar ───────────────────────────────────────────────────────────────────
@client.tree.command(name="buscar", description="Busca recordatorios por texto en el título o descripción")
@app_commands.describe(texto="Texto a buscar")
async def buscar(interaction: discord.Interaction, texto: str):
    try:
        por_tarea = client.supabase.table("recordatorios")\
            .select("*")\
            .eq("usuario_id", interaction.user.id)\
            .eq("completado", False)\
            .ilike("tarea", f"%{texto}%")\
            .order("fecha_ejecucion")\
            .execute()

        por_desc = client.supabase.table("recordatorios")\
            .select("*")\
            .eq("usuario_id", interaction.user.id)\
            .eq("completado", False)\
            .ilike("descripcion", f"%{texto}%")\
            .order("fecha_ejecucion")\
            .execute()

        ids_vistos = set()
        filas = []
        for r in por_tarea.data + por_desc.data:
            if r['id'] not in ids_vistos:
                ids_vistos.add(r['id'])
                filas.append(r)
        filas.sort(key=lambda x: x['fecha_ejecucion'])

        if not filas:
            return await interaction.response.send_message(
                f"No hay recordatorios pendientes con '{texto}'.", ephemeral=True
            )

        embed = discord.Embed(title=f"🔍 Resultados para '{texto}'", color=discord.Color.teal())
        for r in filas:
            fecha_local = _dt_de_str(r['fecha_ejecucion']).astimezone(ZONA_ES)
            embed.add_field(
                name=f"🆔 {r['id']} — {r['tarea']}",
                value=f"🕒 {fecha_local.strftime('%d/%m/%Y a las %H:%M')}\n{r.get('descripcion') or 'Sin detalles'}",
                inline=False
            )
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


# ── Arranque ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    keep_alive()
    client.run(os.getenv("DISCORD_TOKEN"))
