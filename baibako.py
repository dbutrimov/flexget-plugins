# -*- coding: utf-8 -*-
from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import re
from bs4 import BeautifulSoup

import requests


class BaibakoShow(object):
    titles = []
    url = ''

    def __init__(self, titles, url):
        self.titles = titles
        self.url = url


headers = {'cookie': 'PHPSESSID=brq4r5svhlp3h53bkcbgkjda72; uid=91346; pass=177eedfa3b4e41e2595730e6d0a0f39f'}

try:
    serials_response = requests.get('http://baibako.tv/serials.php', headers=headers)
except requests.RequestException as e:
    print(e)
serials_html = serials_response.content

table_class_regexp = re.compile(r'table.*', flags=re.IGNORECASE)

shows = set()

serials_tree = BeautifulSoup(serials_html, 'html.parser')
table_node = serials_tree.find('table', class_=table_class_regexp)
if table_node:
    link_nodes = table_node.find_all('a')
    for link_node in link_nodes:
        title = link_node.get_text()
        href = 'http://baibako.tv/' + link_node.get('href')
        # print("{0} - {1}".format(title.encode('windows-1251', 'replace'), href))

        show = BaibakoShow([title], href)
        shows.add(show)

serial_tab = 'hd720'
search_title = "11.22.63"
for show in shows:
    if search_title not in show.titles:
        continue

    try:
        serial_response = requests.get(show.url + '&tab=' + serial_tab, headers=headers)
    except requests.RequestException as e:
        print(e)
    serial_html = serial_response.content

    serial_tree = BeautifulSoup(serial_html, 'html.parser')
    table_node = serial_tree.find('table', class_=table_class_regexp)
    if table_node:
        href_regexp = re.compile(r'details.php\?id=(\d+)', flags=re.IGNORECASE)
        link_nodes = table_node.find_all('a', href=href_regexp)
        for link_node in link_nodes:
            href = 'http://baibako.tv/' + link_node.get('href')
            title = link_node.get_text()
            print("{0} - {1}".format(title.encode('windows-1251', 'replace'), href))
