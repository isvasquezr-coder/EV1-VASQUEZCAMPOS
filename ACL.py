# Script de ACL
acl = int(input("Introduce el número de ACL IPv4: "))

if (1 <= acl <= 99) or (1300 <= acl <= 1999):
        print(f"La ACL {acl} es una ACL Estándar.")
elif (100 <= acl <= 199) or (2000 <= acl <= 2699):
        print(f"La ACL {acl} es una ACL Extendida.")
else:
        print(f"El número {acl} no corresponde a una lista de acceso.")