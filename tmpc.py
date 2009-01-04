#! /usr/bin/env python
# $Id: tmpc.py 12 2006-11-05 17:20:20Z gavin $

import os
import sys
import time
import timing
import string
import re
import socket
import urllib2
import gtk
import gtk.glade
import gobject
import urllib
import sgmllib
import Image
import md5
import egg.trayicon
from optparse import OptionParser
from SOAPpy import WSDL

import mpdclient2
import pynotify
import feedparser

LAST_FM = {
    'USERNAME' : 'gaving',
    'ARTIST_URL' : 'http://last.fm/music/',
    'TITLE_URL' : '/_/',
    'FRIENDS_FEED'  : 'http://ws.audioscrobbler.com/1.0/user/%s/friends.txt',
    'RECENT_FEED' : 'http://ws.audioscrobbler.com/1.0/user/%s/recenttracks.rss'
}

SETTINGS = {
        'DOWNLOADED_COVERS_PATH' : os.path.join(os.getcwd(),'.covers/'),
        'MUSIC_PATH' : '/mnt/media/music/',
        'MAX_RECENT_ENTRIES' : 10,
}

DEFAULTS = {
    'lastfm': True,
    'lyrics': True,
    'verbosity': 0
}

DEBUG_LVL = 0

def debug(msg):
    if not DEBUG_LVL:
       return
    print "%s %s\n" %(time.strftime("%d%m%y %H:%M:%S"), msg)

def get_hash(str):
    r = ''
    hexStr = string.hexdigits
    for ch in md5.md5(str).digest():
        i = ord(ch)
        r = r + hexStr[(i >> 4) & 0xF] + hexStr[i & 0xF]
    return r

class Lastfm:
    """
    Handles specific queries to http://www.last.fm/ such as grabbing covers,
    artist names, recent tracks, etc.
    """

    page_regex = re.compile(r'<div class="cover">(.*?)<\/div>', re.DOTALL)
    cover_regex = re.compile(r'<img src="(.*?)"', re.DOTALL)

    def __init__(self):
        self.is_enabled = False
        socket.setdefaulttimeout(2)
        self.friends_names = None
        self.friends_names_for_data = None
        self.recent_tracks = {}

    def enable(self):
        self.is_enabled = True

    def get_cover(self, artist, album):
        if not self.is_enabled:
            debug("* Last.fm support disabled, not proceeding.")
            return
        try:
            lastfmurl = "http://www.last.fm/music/%s/%s" \
            % (urllib.quote(artist), urllib.quote(album))
            response = urllib2.urlopen(lastfmurl)
            response_text = response.read()
            img_block_match = self.page_regex.findall(response_text)
            cover_image_url = self.cover_regex.findall(img_block_match[0])[0]

            debug("* Got cover url: %s" % cover_image_url)

            # FIXME: Hackish way to detect bad covers
            if cover_image_url.endswith('no_album_large.gif'):
                debug("* Not saving last.fm placeholder image.")
                return

            response = urllib2.urlopen(cover_image_url)
            
            cover_path = os.path.join(SETTINGS['DOWNLOADED_COVERS_PATH'], 
                    get_hash(album) + ".jpg")
            cover = open(cover_path, 'w')
            cover.write(response.read())
            cover.close()
        except urllib2.HTTPError:
            debug("* Cover not found!")
            return
        except urllib2.URLError:
            debug("* Error fetching cover names!")
            return
        except IOError, e:
            debug("* Couldn't write image to %s!" % cover_path)
            return
        
        return cover_path

    def get_friends_names(self):
        try:
            response = urllib2.urlopen(LAST_FM['FRIENDS_FEED'] % LAST_FM['USERNAME'])
            self.friends_names = [friend.strip() for friend in response.readlines()]
            return self.friends_names
        except (urllib2.HTTPError, urllib2.URLError):
            debug("* Network error fetching friends names!")
            return []

    def sched_friends_data(self):
        debug("* Grabbing each friend with 2 second intervals..")
        self.friends_names_for_data = self.friends_names[:]
        gobject.timeout_add(2000, self.get_friends_data)
        return True
   
    def is_enabled(self):
        return self.is_enabled

    def get_friends_data(self):
        if self.friends_names_for_data:
            friend = self.friends_names_for_data.pop(0)
            debug("** Grabbing recently played tracks for %s" % friend)
            self.recent_tracks[friend] = self.fetch_recent_tracks(friend)
        else:
            self.friends_names_for_data = self.friends_names[:]
            return False

        return True

    def fetch_recent_tracks(self, username):
        feed = feedparser.parse(LAST_FM['RECENT_FEED'] % username)
        return [i.title for i in feed.entries]

    def get_recent_tracks(self, friend):
        if self.recent_tracks.has_key(friend):
            return self.recent_tracks[friend]


class Lyrics:
    """
    Handles the lyric functionality, displaying lyrics in a window.
    """
    
    def __init__(self):
        self.is_enabled = False
        self.proxy = None

    def on_ok_button_clicked_cb(self, widget):
        self.wTree.get_widget("lyrics_dialog").set_property('visible', False)
    
    def enable(self):
        self.wTree = gtk.glade.XML('tmpc.glade')

        dic = { "on_ok_button_clicked" : self.on_ok_button_clicked_cb } 
        self.wTree.signal_autoconnect(dic)
        
        url = 'http://lyricwiki.org/server.php'

        try:
            self.proxy = WSDL.Proxy(url + '?wsdl')
            self.is_enabled = True
        except xml.parsers.expat.ExpatError:
            debug("* WSDL document not well formed, disabling lyrics support.")
            self.is_enabled = False

    def get_lyrics(self, artist, title):
        try:
            if self.proxy.checkSongExists(artist, title):
                info = self.proxy.getSong(artist, title)
                if info:
                    return info
                else:
                    debug("** Song not found :(")
                    return
            else:     
                debug("** Lyrics not found :(")
                return
        except socket.error:
            debug("** Timed out!")
            return

    def is_enabled(self):
        self.is_enabled = True

    def show_lyrics(self, artist, title):
        debug("* Fetching lyrics...")

        info = self.get_lyrics(artist, title)
        if info:
            buffer = gtk.TextBuffer()
            buffer.set_text(info['lyrics'])
            
            self.wTree.get_widget("lyrics_dialog").set_property('visible', True)
            self.wTree.get_widget("lyrics_dialog").set_title('%s lyrics' % (title))
            self.wTree.get_widget("lyrics_text").set_buffer(buffer)
        
class Notify:
    """
    Provides the tray icon and menu, plus the associated popup from pynotify.
    """

    icons = {
            "play": gtk.STOCK_MEDIA_PLAY,
            "stop": gtk.STOCK_MEDIA_STOP,
            "pause": gtk.STOCK_MEDIA_PAUSE
    }

    def __init__(self):
        if not pynotify.init("Init"):
            debug("Error: Failed to initialize pynotify.")
            sys.exit(1)

        parser = OptionParser(usage="%prog [options]", version="0.1",
                              description="Tray based notifier for MPD.")
        parser.add_option('-v', dest='verbosity', help='Display debugging output', 
                action="store_const", const=1)
        parser.add_option('-d', '--disable-lastfm', dest='lastfm', 
                help='Disable lastfm functionality', action="store_false")
        parser.add_option('-l', '--disable-lyrics', dest='lyrics', 
                help='Disable lyrics functionality', action="store_false")
        parser.set_defaults(**DEFAULTS)
        (option_obj, args) = parser.parse_args()
        
        options = {}
        options['verbosity'] = option_obj.verbosity
        options['lastfm'] = option_obj.lastfm
        options['lyrics'] = option_obj.lyrics

        # FIXME: Ewww.
        global DEBUG_LVL
        DEBUG_LVL = options['verbosity']

        print options

        self.track = {}
        self.artist_url = None
        self.title_url = None
        self.uri = None
        self.tray = None
        
        self.menu = None
        self.buttons = None
        self.box = None
        self.n = None

        self.recent_tracks = []
        self.recent_tracks_menu = None
        self.friends_menus = {}
       
        # Initialize 'modules'
        self.lastfm = Lastfm()
        self.lyrics = Lyrics()
            
        debug("* Populating playlist...")
        timing.start()
        self.playlist = mpdclient2.connect().playlistinfo()
        timing.finish()
        debug("..done. (%d seconds)" % timing.seconds())
        
        if options['lastfm']:
            debug("* Enabling last.fm functionality")
            self.lastfm.enable()

            debug("* Fetching recently played tracks")
            self.recent_tracks = self.lastfm.fetch_recent_tracks(LAST_FM['USERNAME'])

            debug("* Starting friend grabs every minute")
            gobject.timeout_add(60000, self.lastfm.sched_friends_data)
        
        if options['lyrics']:
            debug("* Enabling lyrics functionality")
            self.lyrics.enable()

        self.create_tray()
            
        # Check for track change every second
        gobject.timeout_add(1000, self.check_for_change)

    def create_tray(self):
        self.menu = gtk.Menu()
        
        item = gtk.ImageMenuItem(stock_id=gtk.STOCK_MEDIA_NEXT)
        item.connect("activate", lambda *args: mpdclient2.connect().next())
        self.menu.append(item)
        item = gtk.ImageMenuItem(stock_id=gtk.STOCK_MEDIA_PREVIOUS)
        item.connect("activate", lambda *args: mpdclient2.connect().previous())
        self.menu.append(item)
        
        self.menu.append(gtk.SeparatorMenuItem())
        
        if self.lyrics.is_enabled:
            lyrics = gtk.ImageMenuItem("Lyrics")
            img = gtk.Image()
            img.set_from_icon_name('audio-input-microphone', gtk.ICON_SIZE_MENU)
            lyrics.set_image(img)
            lyrics.connect("activate", lambda *args: self.on_lyrics_clicked_cb())
            self.menu.append(lyrics)

        item = gtk.ImageMenuItem(stock_id=gtk.STOCK_EDIT)
        item.connect("activate", lambda *args: self.on_edit_clicked_cb())
        self.menu.append(item)
        
        self.menu.append(gtk.SeparatorMenuItem())
        
        self.recent = gtk.ImageMenuItem("Recent tracks")
        img = gtk.Image()
        img.set_from_stock(gtk.STOCK_JUSTIFY_FILL, gtk.ICON_SIZE_BUTTON)
        self.recent.set_image(img)
        self.menu.append(self.recent)
        self.recent_tracks_menu = gtk.Menu()
        self.recent.set_submenu(self.recent_tracks_menu)
        
        self.album = gtk.ImageMenuItem("Current Album")
        img = gtk.Image()
        img.set_from_stock(gtk.STOCK_JUSTIFY_FILL, gtk.ICON_SIZE_BUTTON)
        self.album.set_image(img)
        self.menu.append(self.album)
        self.current_album_menu = gtk.Menu()
        self.album.set_submenu(self.current_album_menu)
       
        if self.lastfm.is_enabled:
            self.friends = gtk.ImageMenuItem("Friends")
            img = gtk.Image()
            img.set_from_stock(gtk.STOCK_ORIENTATION_PORTRAIT, gtk.ICON_SIZE_BUTTON)
            self.friends.set_image(img)
            self.menu.append(self.friends)
            self.friends_menu = gtk.Menu()
            self.friends.set_submenu(self.friends_menu)
      
            for friend in self.lastfm.get_friends_names():
                item = gtk.MenuItem(friend)
                menus = {
                        'item' : item,
                        'submenu' : gtk.Menu()
                }
                self.friends_menus[friend] = menus
                self.friends_menu.append(item)
                item.set_submenu(self.friends_menus[friend]['submenu'])

        self.menu.append(gtk.SeparatorMenuItem())
        
        item = gtk.ImageMenuItem(stock_id=gtk.STOCK_QUIT)
        item.connect("activate", gtk.main_quit)
        self.menu.append(item)
        
        self.menu.show_all()

        # FIXME: Update this to the latest way of doing it
        self.tray = egg.trayicon.TrayIcon("TrayIcon")
        self.box = gtk.EventBox()
        self.image = gtk.Image()
        self.box.add(self.image)
        self.tray.add(self.box)
        self.tray.show_all()

        self.box.connect("button-press-event", self.on_tray_clicked_cb)
        self.box.connect("scroll-event", self.on_tray_scroll_cb)
        self.box.connect("enter-notify-event", self.on_popup_open_cb)
        self.box.connect("leave-notify-event", self.on_popup_close_cb)
   
    def update_tray(self):
        # Updating state takes precedence over stop/pause, over repeat, over random, etc.
        if self.status['updating']:
            self.image.set_from_icon_name('appointment-soon', gtk.ICON_SIZE_BUTTON)
        elif self.status['state'] == 'stop' or self.status['state'] == 'pause':
            self.image.set_from_stock(self.icons[self.status['state']], gtk.ICON_SIZE_BUTTON)
        elif self.status['repeat']:
            self.image.set_from_icon_name('media-playlist-repeat', gtk.ICON_SIZE_BUTTON)
        elif self.status['random']:
            self.image.set_from_icon_name('media-playlist-shuffle', gtk.ICON_SIZE_BUTTON)
        else:
            self.image.set_from_stock(self.icons[self.status['state']], gtk.ICON_SIZE_BUTTON)

    def on_popup_open_cb(self, widget, event):
        self.show_notification(False)

    def on_popup_close_cb(self, widget, event):
        self.n.close()

    def on_tray_clicked_cb(self, widget, event):
        if event.button == 1:
            if not self.lyrics.is_enabled:
                return
            if self.n:
                self.n.close()
            info = self.lyrics.get_lyrics(self.track['artist'], self.track['title'])
            if info:
                n = pynotify.Notification('Lyrics', info['lyrics'])
                n.set_timeout(0)
                n.show()

        if event.button != 3:
            return
        
        # Create track options depending on status
        self.create_play_pause_items()

        # Create the dynamic menus
        self.create_friends_recent_menu()
        self.create_local_recent_menu()
        self.create_current_album_menu()

        self.menu.set_screen(widget.get_screen())
        self.menu.popup(None, None, self.place_menu, event.button, event.time)

    def create_play_pause_items(self):
        if self.buttons:
            for track in self.buttons.values():
                self.menu.remove(track)
        else:
            self.buttons = {}
        if self.status['state'] == 'stop' or self.status['state'] == 'pause':
            item = gtk.ImageMenuItem(stock_id=gtk.STOCK_MEDIA_PLAY)
            item.connect("activate", lambda *args: mpdclient2.connect().play())
            self.menu.prepend(item)
            self.buttons['play'] = item
        else:
            item = gtk.ImageMenuItem(stock_id=gtk.STOCK_MEDIA_PAUSE)
            item.connect("activate", lambda *args: mpdclient2.connect().pause())
            self.menu.prepend(item)
            self.buttons['pause'] = item
            item = gtk.ImageMenuItem(stock_id=gtk.STOCK_MEDIA_STOP)
            item.connect("activate", lambda *args: mpdclient2.connect().stop())
            self.menu.prepend(item)
            self.buttons['stop'] = item
        self.menu.show_all()

    def on_tray_scroll_cb(self, widget, event):
        conn = mpdclient2.connect()
        current_elapsed = int(conn.status()['time'].split(':')[0])
        current_pos = int(self.track['pos'])
        if event.direction == gtk.gdk.SCROLL_UP:
            debug("* Seeking from %s -> %d" % (current_elapsed,current_elapsed+10))
            conn.seek(current_pos, current_elapsed+10)
        if event.direction == gtk.gdk.SCROLL_DOWN:
            debug("* Seeking from %s -> %d" % (current_elapsed,current_elapsed-10))
            conn.seek(current_pos, current_elapsed-10)

    def place_menu(self, menu):
        (width, height) = menu.size_request()
        (menu_xpos, menu_ypos) = self.tray.window.get_origin()
        menu_xpos = menu_xpos + self.tray.allocation.x
        menu_ypos = menu_ypos + self.tray.allocation.y
        if menu_ypos > self.tray.get_screen().get_height() / 2:
            menu_ypos -= height + 1
        else:
            menu_ypos += self.tray.allocation.height + 1
        x = menu_xpos;
        y = menu_ypos;
        push_in = True;
        return (x, y, push_in)

    def check_for_change(self):
        conn = mpdclient2.connect()
        track = conn.currentsong()
        conn_status = conn.status()
        status = {
                'state' : conn_status['state'],
                'random' : int(conn_status['random']),
                'repeat' : int(conn_status['repeat']),
                'updating' : int(conn_status.has_key('updating_db'))
        }
        if track != self.track or status != self.status:
            self.track = track
            self.status = status

            # Show popups only on a play status change, nothing else!
            if status['state'] == "play" and not status['updating']:
                self.add_recent_track()
                self.show_notification()

            self.update_tray()
            debug(self.track)

        return True
    
    def add_recent_track(self):
        self.recent_tracks.insert(0, self.track)

    def clear_menu(self, menu):
        for track in menu:
            menu.remove(track)

    def create_local_recent_menu(self):
        self.clear_menu(self.recent_tracks_menu)
       
        # Nothing to list, disable the menu item
        if not self.recent_tracks:
            self.recent.set_sensitive(False)
            return
        else:
            self.recent.set_sensitive(True)
            
        menu_tracks = self.recent_tracks[:SETTINGS['MAX_RECENT_ENTRIES']]

        for i, track in enumerate(menu_tracks):
            try:
                if track.has_key('artist') and track.has_key('title'):
                    label = "%d. %s - %s" % (i+1, track['artist'], track['title'])
                else:
                    label = "%d. %s" % (i+1, os.path.basename(track['file']))
            except AttributeError:
                # Oops, not dealing with a track object but just a string
                label = "%d. %s" % (i+1, track)

            item = gtk.ImageMenuItem(label)
            img = gtk.Image()
            img.set_from_icon_name('gnome-mime-audio', gtk.ICON_SIZE_MENU)
            item.set_image(img)
            item.connect("activate", self.on_recent_track_clicked_cb, track)
            self.recent_tracks_menu.append(item)

        self.recent_tracks_menu.append(gtk.SeparatorMenuItem())
        clear = gtk.ImageMenuItem("Clear Recent Tracks")
        img = gtk.Image()
        img.set_from_stock(gtk.STOCK_CLEAR, gtk.ICON_SIZE_MENU)
        clear.set_image(img)
        clear.connect("activate", self.clear_recent_tracks_cb)
        self.recent_tracks_menu.append(clear)
        self.recent_tracks_menu.show_all()

    def on_recent_track_clicked_cb(self, widget, track):
        mpdclient2.connect().playid(int(track["id"]))
        debug("* Playing recent track with id: %d" % int(track["id"]))

    def clear_recent_tracks_cb(self, widget):
        self.recent_tracks = []

    def create_current_album_menu(self):
        self.clear_menu(self.current_album_menu)

        current_album_tracks = self.get_current_album_tracks()

        # Nothing to list, disable the menu item
        if not current_album_tracks:
            self.album.set_sensitive(False)
            return
        else:
            self.album.set_sensitive(True)
            
        for i, track in enumerate(current_album_tracks):
            try:
                label = "%d. %s" % (i+1, track['title'])
            except KeyError:
                label = "%d. %s" % (i+1, os.path.basename(track['file']))
            
            if self.track['file'] == track['file']:
                item = gtk.ImageMenuItem(label)
                img = gtk.Image()
                img.set_from_stock(gtk.STOCK_MEDIA_PLAY, gtk.ICON_SIZE_MENU)
                item.set_image(img)
            else: 
                item = gtk.MenuItem(label)

            item.connect("activate", self.on_play_track_clicked_cb, track['artist'], track['title'])
            self.current_album_menu.append(item)

        self.current_album_menu.show_all()
    
    def create_friends_recent_menu(self):
        for name, menu in self.friends_menus.items():
            tracks = self.lastfm.get_recent_tracks(name)
            if not tracks:
                menu['item'].set_sensitive(False)
                continue
            menu['item'].set_sensitive(True)
            self.clear_menu(menu['submenu'])
            for track in tracks:
                item = gtk.ImageMenuItem(track)
                img = gtk.Image()
                img.set_from_icon_name('gnome-mime-audio', gtk.ICON_SIZE_MENU)
                item.set_image(img)
                matches = track.split(u'\u2013')
                artist = matches[0].strip()
                title = matches[1].strip()
                item.connect("activate", self.on_play_track_clicked_cb, artist, title)
                menu['submenu'].append(item)
                menu['submenu'].show_all()

    def on_play_track_clicked_cb(self, widget, artist, title):
        # This is just... awful.
        for song in self.playlist:
            try:
                if song['artist'] == artist and song['title'] == title:
                    mpdclient2.connect().playid(int(song['id']))
                    debug("* Trying to play album track with id: %d (%s - %s)" % \
                    (int(song['id']), artist, title))
                    break
            except KeyError:
                pass

    def get_current_album_tracks(self):
        try:
            return mpdclient2.connect().search('album', self.track['album'])
        except KeyError:
            pass

    def get_image(self):
        try:
            file_dir = os.path.dirname(self.track['file'])
            real_image = os.path.join(SETTINGS['MUSIC_PATH'], file_dir + "/Folder.jpg")
            target_image = os.path.join(SETTINGS['DOWNLOADED_COVERS_PATH'], 
                    get_hash(self.track['album']) + ".jpg")

            if os.path.exists(target_image):
                debug("* Found cover in .covers")
                uri = os.path.join("file://", target_image)
            elif os.path.exists(real_image):
                debug("* Resizing local Folder.jpg image")
                self.resize_image(real_image, target_image)
                uri = os.path.join("file://", target_image)
            else:
                debug("* Grabbing image from lastFM")
                cover_image = self.lastfm.get_cover(self.track['artist'],
                        self.track['album'])
                if cover_image:
                    debug("** Found and saved image!")
                    self.resize_image(cover_image, cover_image)
                    uri = os.path.join("file://", cover_image)
                else:
                    debug("** No luck with lastFM, reverting to stock icon")
                    uri = gtk.STOCK_DIALOG_QUESTION
        except KeyError:
            debug("** Missing album tag for lookup, reverting to stock icon")
            uri = gtk.STOCK_DIALOG_QUESTION

        debug("** Using uri: %s" % uri)
        return uri

    def get_status(self):
        if self.status['state'] == "play":
            return "Playing"
        elif self.status['state'] == "stop":
            return "Stopped"
        elif self.status['state'] == "pause":
            return "Paused"

    def show_notification(self, timeout=True):
        if self.track.has_key("artist") and self.track.has_key("title"):
            artist = self.track['artist'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;') 
            title = self.track['title'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;') 
            self.artist_url = LAST_FM['ARTIST_URL'] + artist
            self.title_url = LAST_FM['ARTIST_URL'] + artist + LAST_FM['TITLE_URL'] + title
            artist = "<a href='%s'>%s</a>" % (urllib.quote(self.artist_url), artist)
            title = "<a href='%s'>%s</a>" % (urllib.quote(self.title_url), title)
            markup = "%s - %s" % (artist, title)
#            if self.track.has_key("album"):
#                album = self.track['album'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;') 
#                markup += " (.. %s)" % album
        else:
            markup = self.track['file']

        if self.n:
            self.n.close()

        # Create new popup with the image returned 
        self.n = pynotify.Notification(self.get_status(), markup, self.get_image())
        x, y = self.box.window.get_origin()


        time_left = None
        try:
            # Work out the time left of the track and use it as the timeout
            track_length = mpdclient2.connect().status()['time'].split(':')
            time_left = (int(track_length[1]) - int(track_length[0])) * 1000
        except KeyError:
            pass

        self.n.set_timeout(timeout and 3000 or time_left or 3000)
        
        self.n.add_action("media-play", "Queue Album", self.on_queue_album_cb)
        self.n.add_action("properties", "Lyrics", self.on_lyrics_clicked_cb)
        self.n.add_action("edit", "Edit Tag", self.on_edit_clicked_cb)

        self.n.set_hint("x", x)
        self.n.set_hint("y", y)

        if not self.n.show():
            debug("Error: Failed to send notification")

    def resize_image(self, real_image, target_image):
        try:
            image = Image.open(real_image)
            image = image.resize((60, 60), Image.NEAREST)
            image.save(target_image)
        except IOError:
            pass

    def on_lyrics_clicked_cb(self, n=None, action=None):
        self.lyrics.show_lyrics(self.track['artist'], self.track['title'])
        if n:
            n.close()

    def on_edit_clicked_cb(self, n=None, action=None):
        debug("You clicked edit tag")
        if n:
            n.close()
    
    def on_queue_album_cb(self, n=None, action=None):
        debug("You clicked enqueue album")
        if n:
            n.close()

    def run(self):
        try:
            gtk.main()
        except KeyboardInterrupt:
            sys.exit(1)

if __name__ == "__main__":
    notify = Notify()
    notify.run()

# vim: set expandtab shiftwidth=4 softtabstop=4 textwidth=79:
