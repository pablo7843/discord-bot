import os
import discord
from discord import app_commands
from discord.ext import tasks
import datetime
import pytz
import dateparser
import aiosqlite
from dotenv import load_dotenv

load_dotenv()

ZONA_ES = pytz.timezone('Europe/Madrid')
DB_PATH = "data/recordatorios.db"
os.makedirs("data", exist_ok=True)

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


def _dt_de_db(s: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=pytz.utc)


class CalendarBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.db: aiosqlite.Connection = None

    async def setup_hook(self):
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS recordatorios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL,
                tarea TEXT NOT NULL,
                descripcion TEXT DEFAULT '',
                fecha_ejecucion TEXT NOT NULL,
                completado INTEGER DEFAULT 0,
                preaviso_enviado INTEGER DEFAULT 0
            )
        """)
        await self.db.commit()
        self.revisar_alertas.start()

    async def on_ready(self):
        print(f'✅ Bot iniciado como {self.user}')
        await self.tree.sync()
        print("🚀 Comandos sincronizados.")

    async def close(self):
        if self.db:
            await self.db.close()
        await super().close()

    @tasks.loop(minutes=1)
    async def revisar_alertas(self):
        ahora_utc = datetime.datetime.now(pytz.utc)

        # Recordatorios que ya tocaron
        async with self.db.execute(
            "SELECT * FROM recordatorios WHERE completado=0 AND fecha_ejecucion <= ?",
            (ahora_utc.isoformat(),)
        ) as cur:
            filas = await cur.fetchall()

        for r in filas:
            embed = discord.Embed(
                title="⏰ ¡RECORDATORIO!",
                description=f"**{r['tarea']}**",
                color=discord.Color.red()
            )
            if r['descripcion']:
                embed.add_field(name="Detalles", value=r['descripcion'], inline=False)
            try:
                user = await self.fetch_user(int(r['usuario_id']))
                if user:
                    await user.send(embed=embed)
            except Exception as e:
                print(f"Error enviando recordatorio {r['id']}: {e}")
            await self.db.execute(
                "UPDATE recordatorios SET completado=1 WHERE id=?", (r['id'],)
            )
        if filas:
            await self.db.commit()

        # Pre-avisos: ventana de 25-35 min antes (se envía una sola vez por preaviso_enviado)
        ventana_ini = (ahora_utc + datetime.timedelta(minutes=25)).isoformat()
        ventana_fin = (ahora_utc + datetime.timedelta(minutes=35)).isoformat()

        async with self.db.execute(
            """SELECT * FROM recordatorios
               WHERE completado=0 AND preaviso_enviado=0
               AND fecha_ejecucion >= ? AND fecha_ejecucion <= ?""",
            (ventana_ini, ventana_fin)
        ) as cur:
            preaviso_filas = await cur.fetchall()

        for r in preaviso_filas:
            try:
                user = await self.fetch_user(int(r['usuario_id']))
                if user:
                    fecha_local = _dt_de_db(r['fecha_ejecucion']).astimezone(ZONA_ES)
                    embed = discord.Embed(
                        title="🔔 Recordatorio en ~30 minutos",
                        description=f"**{r['tarea']}**\n🕒 A las {fecha_local.strftime('%H:%M')}",
                        color=discord.Color.yellow()
                    )
                    await user.send(embed=embed)
            except Exception as e:
                print(f"Error enviando pre-aviso {r['id']}: {e}")
            await self.db.execute(
                "UPDATE recordatorios SET preaviso_enviado=1 WHERE id=?", (r['id'],)
            )
        if preaviso_filas:
            await self.db.commit()

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

    await client.db.execute(
        "INSERT INTO recordatorios (usuario_id, tarea, descripcion, fecha_ejecucion) VALUES (?, ?, ?, ?)",
        (interaction.user.id, tarea, descripcion, fecha_dt.isoformat())
    )
    await client.db.commit()

    fecha_local = fecha_dt.astimezone(ZONA_ES).strftime('%d/%m/%Y a las %H:%M')
    embed = discord.Embed(title="✅ Recordatorio guardado", color=discord.Color.green())
    embed.add_field(name="Tarea", value=tarea, inline=False)
    embed.add_field(name="Cuándo (España)", value=fecha_local, inline=False)
    if descripcion:
        embed.add_field(name="Descripción", value=descripcion, inline=False)
    await interaction.response.send_message(embed=embed)


# ── /listar ───────────────────────────────────────────────────────────────────
@client.tree.command(name="listar", description="Muestra todos tus recordatorios pendientes")
async def listar(interaction: discord.Interaction):
    async with client.db.execute(
        "SELECT * FROM recordatorios WHERE usuario_id=? AND completado=0 ORDER BY fecha_ejecucion",
        (interaction.user.id,)
    ) as cur:
        filas = await cur.fetchall()

    if not filas:
        return await interaction.response.send_message(
            "No tienes recordatorios pendientes.", ephemeral=True
        )

    embed = discord.Embed(title="📅 Tus recordatorios pendientes", color=discord.Color.blue())
    for r in filas:
        fecha_local = _dt_de_db(r['fecha_ejecucion']).astimezone(ZONA_ES)
        embed.add_field(
            name=f"🆔 {r['id']} — {r['tarea']}",
            value=f"🕒 {fecha_local.strftime('%d/%m/%Y a las %H:%M')}",
            inline=False
        )
    await interaction.response.send_message(embed=embed)


# ── /hoy ──────────────────────────────────────────────────────────────────────
@client.tree.command(name="hoy", description="Tareas programadas para hoy")
async def hoy(interaction: discord.Interaction):
    ahora_es = datetime.datetime.now(ZONA_ES)
    inicio = ahora_es.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(pytz.utc)
    fin = inicio + datetime.timedelta(days=1)

    async with client.db.execute(
        """SELECT * FROM recordatorios WHERE usuario_id=?
           AND fecha_ejecucion >= ? AND fecha_ejecucion < ?
           ORDER BY fecha_ejecucion""",
        (interaction.user.id, inicio.isoformat(), fin.isoformat())
    ) as cur:
        filas = await cur.fetchall()

    embed = discord.Embed(
        title=f"📌 {DIAS_ES[ahora_es.weekday()]} {ahora_es.strftime('%d/%m/%Y')}",
        color=discord.Color.orange()
    )
    if not filas:
        embed.description = "No tienes nada programado para hoy. 🎉"
    else:
        for r in filas:
            hora = _dt_de_db(r['fecha_ejecucion']).astimezone(ZONA_ES).strftime('%H:%M')
            estado = "✅" if r['completado'] else "⏳"
            embed.add_field(
                name=f"{estado} {hora} — {r['tarea']}",
                value=r['descripcion'] or "Sin detalles",
                inline=False
            )
    await interaction.response.send_message(embed=embed)


# ── /semana ───────────────────────────────────────────────────────────────────
@client.tree.command(name="semana", description="Agenda de los próximos 7 días")
async def semana(interaction: discord.Interaction):
    ahora_es = datetime.datetime.now(ZONA_ES)
    inicio = ahora_es.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(pytz.utc)
    fin = inicio + datetime.timedelta(days=7)

    async with client.db.execute(
        """SELECT * FROM recordatorios WHERE usuario_id=? AND completado=0
           AND fecha_ejecucion >= ? AND fecha_ejecucion < ?
           ORDER BY fecha_ejecucion""",
        (interaction.user.id, inicio.isoformat(), fin.isoformat())
    ) as cur:
        filas = await cur.fetchall()

    embed = discord.Embed(title="📆 Agenda — próximos 7 días", color=discord.Color.purple())
    if not filas:
        embed.description = "No tienes nada para esta semana. ✨"
    else:
        dia_actual = None
        for r in filas:
            fecha_local = _dt_de_db(r['fecha_ejecucion']).astimezone(ZONA_ES)
            dia_str = f"{DIAS_ES[fecha_local.weekday()]} {fecha_local.strftime('%d/%m')}"
            if dia_str != dia_actual:
                dia_actual = dia_str
                embed.add_field(name=f"── {dia_str} ──", value="​", inline=False)
            embed.add_field(
                name=f"🆔{r['id']} {fecha_local.strftime('%H:%M')} — {r['tarea']}",
                value=r['descripcion'] or "Sin detalles",
                inline=False
            )
    await interaction.response.send_message(embed=embed)


# ── /eliminar ─────────────────────────────────────────────────────────────────
@client.tree.command(name="eliminar", description="Elimina un recordatorio por su ID")
@app_commands.describe(id_tarea="ID del recordatorio (usa /listar para verlos)")
async def eliminar(interaction: discord.Interaction, id_tarea: int):
    async with client.db.execute(
        "SELECT usuario_id FROM recordatorios WHERE id=?", (id_tarea,)
    ) as cur:
        fila = await cur.fetchone()

    if not fila or int(fila['usuario_id']) != interaction.user.id:
        return await interaction.response.send_message(
            "❌ No encontrado o sin permiso.", ephemeral=True
        )

    await client.db.execute("DELETE FROM recordatorios WHERE id=?", (id_tarea,))
    await client.db.commit()
    await interaction.response.send_message(f"🗑️ Recordatorio `{id_tarea}` eliminado.")


# ── /editar ───────────────────────────────────────────────────────────────────
@client.tree.command(name="editar", description="Cambia la fecha o descripción de un recordatorio")
@app_commands.describe(
    id_tarea="ID del recordatorio",
    cuando="Nueva fecha/hora (opcional): 'el lunes a las 10', 'en 3 horas'",
    descripcion="Nueva descripción (opcional)"
)
async def editar(interaction: discord.Interaction, id_tarea: int, cuando: str = "", descripcion: str = ""):
    async with client.db.execute(
        "SELECT * FROM recordatorios WHERE id=? AND usuario_id=?",
        (id_tarea, interaction.user.id)
    ) as cur:
        fila = await cur.fetchone()

    if not fila:
        return await interaction.response.send_message(
            "❌ No encontrado o sin permiso.", ephemeral=True
        )
    if not cuando and not descripcion:
        return await interaction.response.send_message(
            "Indica al menos una cosa a cambiar: `cuando` o `descripcion`.", ephemeral=True
        )

    nueva_fecha = None
    if cuando:
        nueva_fecha = _parsear_fecha(cuando)
        if not nueva_fecha:
            return await interaction.response.send_message(
                "❌ No entendí la nueva fecha.", ephemeral=True
            )
        await client.db.execute(
            "UPDATE recordatorios SET fecha_ejecucion=?, preaviso_enviado=0 WHERE id=?",
            (nueva_fecha.isoformat(), id_tarea)
        )
    if descripcion:
        await client.db.execute(
            "UPDATE recordatorios SET descripcion=? WHERE id=?", (descripcion, id_tarea)
        )
    await client.db.commit()

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


# ── /posponer ─────────────────────────────────────────────────────────────────
@client.tree.command(name="posponer", description="Pospone un recordatorio a una nueva fecha/hora")
@app_commands.describe(
    id_tarea="ID del recordatorio",
    cuando="Nueva fecha: 'en 1 hora', 'en 30 minutos', 'mañana a las 10'"
)
async def posponer(interaction: discord.Interaction, id_tarea: int, cuando: str):
    async with client.db.execute(
        "SELECT usuario_id FROM recordatorios WHERE id=?", (id_tarea,)
    ) as cur:
        fila = await cur.fetchone()

    if not fila or int(fila['usuario_id']) != interaction.user.id:
        return await interaction.response.send_message(
            "❌ No encontrado o sin permiso.", ephemeral=True
        )

    nueva_fecha = _parsear_fecha(cuando)
    if not nueva_fecha:
        return await interaction.response.send_message(
            "❌ No entendí. Prueba: 'en 1 hora', 'en 30 minutos', 'mañana a las 10'.", ephemeral=True
        )

    await client.db.execute(
        "UPDATE recordatorios SET fecha_ejecucion=?, completado=0, preaviso_enviado=0 WHERE id=?",
        (nueva_fecha.isoformat(), id_tarea)
    )
    await client.db.commit()

    fecha_local = nueva_fecha.astimezone(ZONA_ES).strftime('%d/%m/%Y a las %H:%M')
    await interaction.response.send_message(
        f"⏩ Recordatorio `{id_tarea}` pospuesto hasta el **{fecha_local}**."
    )


# ── /completar ────────────────────────────────────────────────────────────────
@client.tree.command(name="completar", description="Marca un recordatorio como completado manualmente")
@app_commands.describe(id_tarea="ID del recordatorio")
async def completar(interaction: discord.Interaction, id_tarea: int):
    async with client.db.execute(
        "SELECT usuario_id FROM recordatorios WHERE id=?", (id_tarea,)
    ) as cur:
        fila = await cur.fetchone()

    if not fila or int(fila['usuario_id']) != interaction.user.id:
        return await interaction.response.send_message(
            "❌ No encontrado o sin permiso.", ephemeral=True
        )

    await client.db.execute("UPDATE recordatorios SET completado=1 WHERE id=?", (id_tarea,))
    await client.db.commit()
    await interaction.response.send_message(f"✅ Recordatorio `{id_tarea}` marcado como completado.")


# ── /buscar ───────────────────────────────────────────────────────────────────
@client.tree.command(name="buscar", description="Busca recordatorios por texto en el título o descripción")
@app_commands.describe(texto="Texto a buscar")
async def buscar(interaction: discord.Interaction, texto: str):
    patron = f"%{texto}%"
    async with client.db.execute(
        """SELECT * FROM recordatorios WHERE usuario_id=? AND completado=0
           AND (tarea LIKE ? OR descripcion LIKE ?)
           ORDER BY fecha_ejecucion""",
        (interaction.user.id, patron, patron)
    ) as cur:
        filas = await cur.fetchall()

    if not filas:
        return await interaction.response.send_message(
            f"No hay recordatorios pendientes con '{texto}'.", ephemeral=True
        )

    embed = discord.Embed(title=f"🔍 Resultados para '{texto}'", color=discord.Color.teal())
    for r in filas:
        fecha_local = _dt_de_db(r['fecha_ejecucion']).astimezone(ZONA_ES)
        embed.add_field(
            name=f"🆔 {r['id']} — {r['tarea']}",
            value=f"🕒 {fecha_local.strftime('%d/%m/%Y a las %H:%M')}\n{r['descripcion'] or 'Sin detalles'}",
            inline=False
        )
    await interaction.response.send_message(embed=embed)


# ── Arranque ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    client.run(os.getenv("DISCORD_TOKEN"))
