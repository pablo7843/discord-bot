# Usamos una imagen de Python ligera
FROM python:3.11-slim

# Evita que Python genere archivos .pyc y permite que los logs salgan directos
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Establecemos el directorio de trabajo
WORKDIR /app

# Instalamos dependencias del sistema necesarias para algunas librerías
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copiamos solo el archivo de requerimientos primero (para aprovechar la caché de Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos el resto del código
COPY . .

# Comando para arrancar el bot
CMD ["python", "main.py"]