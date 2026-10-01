# Usa uma imagem estável e leve do Python
FROM python:3.11-slim

# Evita que o Python escreva arquivos .pyc e força o output direto no terminal
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Define o diretório de trabalho dentro do container
WORKDIR /app

# Instala dependências do sistema necessárias para compilação básica e Git
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Instala o gerenciador 'uv' para acelerar drasticamente o download do PyTorch (CPU) e do Laya
RUN pip install --no-cache-dir uv

# Instala o PyTorch otimizado para CPU e o pacote Laya com o extra para servir a API
RUN uv pip install --system --torch-backend=cpu "laya[serve]"

# Expõe a porta padrão que o servidor do Laya utiliza
EXPOSE 8000

# Comando para iniciar o servidor embutido do Laya. 
# O parâmetro --preload baixa os pesos do modelo (Hugging Face) na inicialização.
CMD ["python", "-m", "laya.serve", "--host", "0.0.0.0", "--port", "8000", "--preload", "--api-style", "jev"]

