import urllib.request

url = "http://172.22.10.91:8001/check_review_cases.py"
target = "/tmp/check_review_cases.py"

urllib.request.urlretrieve(url, target)
print("Downloaded:", target)
