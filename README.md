# flexget-plugins-ruvoice

**flexget-plugins-ruvoice** is the bundle of search/urlrewrite pugins for FlexGet.

---

## LostFilm

Web site: [lostfilm.tv](http://lostfilm.tv)

### Configuration

#### Authorization

```yaml
lostfilm_auth:
  username: 'username_here'
  password: 'password_here'
```

#### UrlRewrite

```yaml
lostfilm:
  regexp: '720p'
```

#### Search

```yaml
lostfilm: yes
```

---

## NewStudio

Web site: [newstudio.tv](http://newstudio.tv)

### Configuration

#### Authorization

```yaml
newstudio_auth:
  username: 'username_here'
  password: 'password_here'
```

---

## BaibaKo

Web site: [baibako.tv](http://baibako.tv)

### Configuration

#### Authorization

```yaml
baibako_auth:
  username: 'username_here'
  password: 'password_here'
```

#### Search

```yaml
baibako: yes
```

```yaml
baibako:
  serial_tab: 'all'  # 'hd720', 'hd1080', 'x264', 'xvid' or 'all' (default)
```

---

## AlexFilm

Web site: [alexfilm.cc](http://alexfilm.cc)

### Configuration

#### Authorization

```yaml
alexfilm_auth:
  username: 'username_here'
  password: 'password_here'
```
