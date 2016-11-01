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


headers = {'cookie': 'PHPSESSID=updjhuc3mlac37ejge5it5kk51; uid=91346; pass=065333565042aca10d1e0aeccc477e20'}

try:
    serials_response = requests.get('http://baibako.tv/serials.php', headers=headers)
except requests.RequestException as e:
    print(e)
serials_html = serials_response.text

table_class_regexp = re.compile(r'table.*', flags=re.IGNORECASE)
episode_title_regexp = re.compile(r'^([^/]*?)\s*/([^/]*?)\s*/\s*s(\d+)e(\d+)\s*/\s*([^/]*?)\s*/.*$', flags=re.IGNORECASE)

shows = set()

serials_tree = BeautifulSoup(serials_html, 'html.parser')
table_node = serials_tree.find('table', class_=table_class_regexp)
if table_node:
    link_nodes = table_node.find_all('a')
    for link_node in link_nodes:
        title = link_node.text
        href = 'http://baibako.tv/' + link_node.get('href')
        # print("{0} - {1}".format(title.encode('windows-1251', 'replace'), href))

        show = BaibakoShow([title], href)
        shows.add(show)

print("{0} show(s) found!".format(len(shows)))

serial_tab = 'hd720'
search_title = "Скорпион"  # "11.22.63"
print(search_title)
for show in shows:
    if search_title not in show.titles:
        continue

    serial_url = show.url + '&tab=' + serial_tab
    print(serial_url)
    try:
        serial_response = requests.get(serial_url, headers=headers)
    except requests.RequestException as e:
        print(e)
    serial_html = serial_response.text

    serial_tree = BeautifulSoup(serial_html, 'html.parser')
    table_node = serial_tree.find('table', class_=table_class_regexp)
    if table_node:
        href_regexp = re.compile(r'details.php\?id=(\d+)', flags=re.IGNORECASE)
        link_nodes = table_node.find_all('a', href=href_regexp)
        for link_node in link_nodes:
            href = 'http://baibako.tv/' + link_node.get('href')

            href_match = href_regexp.search(href)
            topic_id = href_match.group(1)
            print("topic_id: {0}".format(topic_id))

            download_url = "http://baibako.tv/download.php?id={0}".format(topic_id)
            print(download_url)

            episode_title = link_node.text

            episode_match = episode_title_regexp.match(episode_title)
            if episode_match:
                ru_title = episode_match.group(1)
                title = episode_match.group(2)
                season = int(episode_match.group(3))
                episode = int(episode_match.group(4))
                quality = episode_match.group(5)

                print("{0} / {1} / s{2:02d}e{3:02d} / {4} - {5}".format(title, ru_title, season, episode, quality, href))
    else:
        print("table node not found")
