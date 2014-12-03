import requests
from time import time

host = 'http://localhost:6543/'
output_dir = './'

def write_capture(response, name):
    f = open(output_dir + name + '.html', 'w')
    f.write(response.text.encode(response.encoding))
    f.close()

def login_user():
    session = requests.Session()
    response = session.post(host + 'login', data = {'userid': 'admin', 'password': 'admin', 'login': 'login'})
    write_capture(response, 'login')
    return session


def hammer_time(session):
    response = session.get(host + 'arsmote-for-ffl/reload_data.json')
    write_capture(response, 'hammer')
    while 1:
        start = time()
        for i in range(10):
            session.get(host + 'arsmote-for-ffl/reload_data.json')
        print "10 get: %s" % (time() - start)

    
if __name__ == '__main__':
    session = login_user()
    hammer_time(session)
