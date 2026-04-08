import json
import requests
api_url = "http://localhost:58000/api/v1/ticket"

headers = {
    "content-type": "application/json"
}

body_json = {
    "username": "cisco",
    "password": "cisco123!"  
}

resp = requests.post(api_url, json.dumps(body_json), headers=headers, verify=False)

print("Ticket request status: ", resp.status_code)
response_json = resp.json()

serviceTicket = response_json["response"]["serviceTicket"]
print("The service ticket number is: ", serviceTicket) 
