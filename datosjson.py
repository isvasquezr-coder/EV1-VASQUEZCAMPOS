import json

with open('myfile.json', 'r') as json_file:
    ourjson = json.load(json_file)

print("Token encontrado: " + ourjson['access_token'])
print("El token caduca en: " + str(ourjson['expires_in']) + " segundos")
