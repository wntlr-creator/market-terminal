import urllib.request, json, sys
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
def get(u):
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=20)
        return r.status, r.read().decode("utf-8", "ignore")
    except Exception as e:
        return str(e), ""
for u in sys.argv[1:]:
    st, b = get(u)
    print("=====", u, st, len(b)); print(b[:2500])
