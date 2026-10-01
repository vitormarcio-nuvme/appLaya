import requests

url = "http://localhost:8000/v1/systemone"
payload = {
    "state": "Quero cancelar minha assinatura e pedir estorno",
    "questions": {
        "categoria": {
            "type": "choice",
            "instructions": "Classifique a intenção principal do cliente de acordo com o texto fornecido.",
            "criteria": ["suporte_financeiro", "duvida_produto", "elogio"]
        }
    }
}

response = requests.post(url, json=payload)
result = response.json()

escolha = result["answers"]["categoria"]["choice"]
print(f"Categoria Escolhida: {escolha}")
# Saída: Categoria Escolhida: suporte_financeiro
