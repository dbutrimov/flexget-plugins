import urllib.request
from lxml import html
# from lxml import etree
# from bs4 import BeautifulSoup
import re

onclick_pattern = re.compile(r'\(\\\'(.+)\\\',\s*\\\'(.+)\\\',\s*\\\'(.+)\\\'\)')
replace_location_pattern = re.compile(r'location\.replace\("(.+)"\);')

cookie = 'uid=821592; pass=cdce3f7776545ec7731de19a5706dbef'
url = 'http://www.lostfilm.tv/details.php?id=19716'

user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/53.0.2785.143 Safari/537.36'

headers = [('Host', 'www.lostfilm.tv'), ('User-Agent', user_agent), ('Cookie', cookie)]

handlers = [urllib.request.ProxyHandler(), urllib.request.HTTPRedirectHandler(),
            urllib.request.HTTPHandler()]

opener = urllib.request.build_opener(*handlers)
opener.addheaders = headers
response = opener.open(url)
h = response.read()

#soup = BeautifulSoup(str(h), 'html.parser')

tree = html.fromstring(str(h))
nodes = tree.xpath('//div[@class="mid"]')
for node in nodes:
    onclick_nodes = node.xpath('.//a[@class="a_download" and starts-with(@onclick, "ShowAllReleases")]/@onclick')
    for onclick_node in onclick_nodes:
        # http://www.lostfilm.tv/nrdr2.php?c=252&s=2.00&e=14
        onclick = onclick_node
        match = onclick_pattern.search(onclick_node)
        c = match.group(1)
        s = match.group(2)
        e = match.group(3)
        u = 'http://www.lostfilm.tv/nrdr2.php?c=' + c + '&s=' + s + '&e=' + e

        response2 = opener.open(u)
        h2 = response2.read()

        html2 = str(h2)

        # location.replace("http://retre.org/?c=252&s=2.00&e=14&u=821592&h=c8812d72bbcab0bc3760e25785efe6df");

        match2 = replace_location_pattern.search(html2)
        redirect_url = match2.group(1)
        print(redirect_url)

        response3 = opener.open(redirect_url)
        h3 = response3.read()

        f = open('c' + c + 's' + s + 'e' + e + '.html', 'wb')
        f.write(h3)
        f.close()

