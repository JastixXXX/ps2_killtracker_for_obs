# To use this:
# install python and requirements.txt (pip install -r requirements.txt)
# get a service ID here https://census.daybreakgames.com/ and insert it into "SERVICE_ID"
# put desired characters into the NAME_LIST array in any case like in the example
# put the desired gun names into GUN_LIST exactly like they are called in the game
# only one gun can be tracked for each character
# add a text source in the OBS, pick it
# add start/stop hotkeys if needed - Htk start/stop kills counter

import obspython as obs
import websockets
import asyncio
import json
import threading
from contextlib import contextmanager
from requests import get

# ============== put your data here =====================

NAME_LIST: list = ['aliantares', 'leopoldlocust', 'konradkestrel'] # all tracked character names
GUN_LIST: list = ['M20 Kestrel', 'Antares LC', 'M18 Locust'] # all tracked guns
SERVICE_ID = '<your_id>'
# =======================================================

# auto release context managers
@contextmanager
def source_ar(source_name):
    source = obs.obs_get_source_by_name(source_name)
    try:
        yield source
    finally:
        obs.obs_source_release(source)


@contextmanager
def data_ar(source_settings=None):
    if not source_settings:
        settings = obs.obs_data_create()
    if source_settings:
        settings = obs.obs_source_get_settings(source_settings)
    try:
        yield settings
    finally:
        obs.obs_data_release(settings)


_LOOP: asyncio.AbstractEventLoop | None = None
_THREAD: threading.Thread | None = None
NAMES_AND_IDS: dict = {} # mapping of names and IDs. Census even streaming works only with IDs
CHAR_ONLINE: str | None = None # a name from NAME_LIST which is currently online
CURRENT_GUN: str | None = None # a gun from GUN_LIST which exists in CHAR_ONLINE's stats
CURRENT_GUN_ID: str | None = None # the id of a current gun
CENSUS_LOOP_RUNNING_OK: bool = False # allows or not to connect to census and receive messages
URL: str = f'https://census.daybreakgames.com/s:{SERVICE_ID}/get/ps2:v2/' # census URL
KILLS: int | None = None # kills counter
# OBS will crash if we try to put text into non existing text source
# It can happen if the text source was saved once and then deleted at the next launch.
# if not True, the algorithm won't be allowed to run
SOURCE: str | None = None
LEADERS = [] # contains an array of tuples (name, kills) up to CHAR_ONLINE or to 10 items
GOAL_KILLS: int | None = None # the maximum of kills done by a current weapon sp far
CLOSEST_KILLS: int | None = None # the amount of kills of a player one tier above


# FUNCTIONS TO WORK WITH CENSUS!!

def get_online_char() -> None:
    """Requests census with all characters from NAME_LIST. Fills NAMES_AND_IDS
    with mappings of characters names and IDs, also changes CHAR_ONLINE
    if any character is online"""

    global CHAR_ONLINE, NAMES_AND_IDS
    # params for census request
    params = {
        'name.first_lower': ','.join([ name.lower() for name in NAME_LIST ]),
        'c:join': 'characters_online_status^on:character_id^to:character_id^inject_at:online',
        'c:limit': len(NAME_LIST),
    }
    try:
        resp = get(URL + 'character_name', params=params).json()
        # filling up NAMES_AND_IDS for sure and CHAR_ONLINE if any of them is online
        for character in resp['character_name_list']:
            NAMES_AND_IDS[character['name']['first']] = character['character_id']
            if character['online']['online_status'] != '0':
                CHAR_ONLINE = character['name']['first']
    # there could be a few errors, in this situation it doesn't matter what happened
    # matters that we didn't get the data. Character IDs are needed
    except Exception as e:
        print('Error occured:', e)


def get_stats() -> dict:
    """Requests census for stats of the currently online character,
    looks for weapon_kills field and for any weapon from GUN_LIST"""
    
    def deep_get(d: dict | None, keys: list, default=None):
        """
        Gets a value from dicts with deep nesting or default
        Example:
            d = {'meta': {'status': 'OK', 'status_code': 200}}
            deep_get(d, ['meta', 'status_code'])          # => 200
            deep_get(d, ['garbage', 'status_code'])       # => None
            deep_get(d, ['meta', 'garbage'], default='-') # => '-'
        """
        # Since we are getting nested field, we have to get the whole sequence of nested keys, at least list of two values
        assert type(keys) is list
        # if one of the keys in the list of keys is absent then the requested values is absebt. return default
        if d is None:
            return default
        # If not keys anymore, then we found the needed value
        if not keys:
            return d
        # recursive call of the same function with one level less nesting
        return deep_get(d.get(keys[0]), keys[1:], default)

    global CURRENT_GUN, KILLS, CURRENT_GUN_ID
    # params for census request
    params = {
        'name.first': CHAR_ONLINE,
        'c:resolve': 'weapon_stat_by_faction(stat_name,item_id,value_vs,value_tr,value_nc)',
        'c:join': ['faction^inject_at:faction_name^show:code_tag',
        'item^on:stats.weapon_stat_by_faction.item_id^to:item_id^inject_at:weapon_name^show:name.en',
        ],
    }
    try:
        resp = get(URL + 'character', params=params).json()
        if resp.get('returned'):
            # weapon_stat_by_faction table returns a lot of records with different stats of different guns
            # we take the first one (and hopefully the only one) which is named weapon_kills
            # and has one of the listed in GUN_LIST guns
            item = None
            for item in resp['character_list'][0]['stats']['weapon_stat_by_faction']:
                if deep_get(item, ['weapon_name', 'name', 'en']) in GUN_LIST and item.get('stat_name') == 'weapon_kills':
                    break
            # if the desired row was found
            if item:
                # removing teamkills from the stats
                faction_tags = ['vs', 'tr', 'nc']
                faction_tags.remove(resp['character_list'][0]['faction_name']['code_tag'].lower())
                # preserving gun's name and ID
                CURRENT_GUN = item['weapon_name']['name']['en']
                CURRENT_GUN_ID = item['item_id']
                # and summ total kills from kills on other two factions
                KILLS = sum([ int(item.get('value_' + tag, 0)) for tag in faction_tags ])
                print(f'<get_stats> {CURRENT_GUN} kills: {KILLS}')
    # there could be a few errors, in this situation it doesn't matter what happened
    # matters that we didn't get the data. Gun name, id and stats are needed
    except Exception as e:
        print('Error occured:', e)    


def get_leaders() -> None:
    """if CURRENT_GUN_ID is determined, requests voidwell api for
    this guns leaderboard. Saves data to global variables"""
    
    # needs both values to determine what leaderboard to take ans up to what characetr
    if not CURRENT_GUN_ID or not CHAR_ONLINE:
        print('<get_leaders> cant retrieve leaders, the gun ID is absent or no characters online')
        return
    global LEADERS, GOAL_KILLS, CLOSEST_KILLS
    # takes only page 0 by default. If CHAR_ONLINE is not on this page, then makes no sense to show the leaderboard
    params = {
        'page': 0,
        'sort': 'kills',
        'sortDir': 'desc',
    }
    try:
        # request to voidewell. If fails - the algorithm will still work, but without the leaderboard
        resp = get('https://api.voidwell.com/ps2/leaderboard/weapon/' + CURRENT_GUN_ID, params=params).json()
        # it supposed to recieve a list
        if not isinstance(resp, list):
            print('<get_leaders> voidwell returned some shit, no leaderboard')
            return
        # record only names and kills into the resulting list
        # stop when got 10 records or found our CHAR_ONLINE
        for num, item in enumerate(resp):
            if len(LEADERS):
                CLOSEST_KILLS = LEADERS[-1][1]
            LEADERS.append((item.get('name', 'n/a'), item.get('kills', 0)))
            if num > 10:
                break
            if item.get('name', 'n/a') == CHAR_ONLINE:
                break
        if len(LEADERS) > 1:
            GOAL_KILLS = LEADERS[0][1]
    # there could be a few errors, in this situation it doesn't matter what happened
    # matters that we didn't get the data. Gun name, id and stats are needed
    except Exception as e:
        print('Error occured:', e)        


async def connect_census():
    """Connects to census, subscribes to events and processes them"""

    global CENSUS_LOOP_RUNNING_OK, KILLS, CHAR_ONLINE
    # before the census connection restore all variables and fetch stats
    global_values_to_default()
    get_online_char()
    if CHAR_ONLINE:
        get_stats()
        get_leaders()
    if not NAMES_AND_IDS:
        print(f'<run_stuff> character IDs or gun ID wasnt received from census, try to restart')
        CENSUS_LOOP_RUNNING_OK = False
        return
    update_text(SOURCE, string_prepare())
    try:
        print('<connect_census> connecting to census...')
        census = await websockets.connect( f"wss://push.planetside2.com/streaming?environment=ps2&service-id=s:{SERVICE_ID}" )
        if census.open:
            print('SUBSCRIBING')
            # sends subscription string
            await census.send(json.dumps({
            	'service': 'event',
            	'action': 'subscribe',
            	'characters': list(NAMES_AND_IDS.values()),
            	'eventNames':['Death', 'PlayerLogin', 'PlayerLogout', 'VehicleDestroy']
            }))
            # endless loop while connected to census
            while census.open:
                # waiting for a message
                message = await census.recv()
                # print('hi from thread: ', threading.get_ident())
                # make a dict out of string
                match json.loads(message):
                    # if it's a subscription acknowledgement, no need to process it, but show the message in the log
                    case {'subscription': subs}:
                        print(f'<connect_census> successfully subscribed for {subs["characterCount"]} characters')
                    # actuall event information
                    case {'payload': payload}:
                        # death event. We need only attacker and with the exact gun
                        if 'attacker_character_id' in payload:
                            if payload.get('attacker_character_id') == NAMES_AND_IDS.get(CHAR_ONLINE) and \
                                payload.get('attacker_weapon_id') == CURRENT_GUN_ID and \
                                payload.get('attacker_team_id') != payload.get('team_id'):
                                KILLS += 1
                                print(f'<connect_census> +1 kill = {KILLS}')
                                if CLOSEST_KILLS is not None and  KILLS > CLOSEST_KILLS and len(LEADERS) > 1:
                                    del LEADERS[-2]
                                    if len(LEADERS) > 1:
                                        CLOSEST_KILLS = LEADERS[-2][1]
                                # renew info on the screen
                                update_text(SOURCE, string_prepare())
                        elif ('event_name') in payload:
                            # logged out event, all the global values should become default
                            if payload['event_name'] == 'PlayerLogout':
                                print(f'<connect_census> {CHAR_ONLINE} logout')
                                global_values_to_default()
                                update_text(SOURCE, string_prepare())
                            # log in event. Stats of the new online character should be fetched
                            elif payload['event_name'] == 'PlayerLogin':
                                for k, v in NAMES_AND_IDS.items():
                                    if v == payload['character_id']:
                                        CHAR_ONLINE = k
                                print(f'<connect_census> {CHAR_ONLINE} login')
                                get_stats()
                                get_leaders()
                                update_text(SOURCE, string_prepare())
                                if not CURRENT_GUN_ID:
                                    CENSUS_LOOP_RUNNING_OK = False
                                    print('<connect_census> couldnt retrieve the current gun ID, please restart')
                                    await census.close()
        print('<connect_census> connection to census lost!')
    except Exception as e:
        print('<connect_census> connect_census connection error: ', e)


async def census_loop():
    """Function to reconnest to census. It waits longer between new reconnect attempts,
    untill reaches 5000 seconds. CENSUS_LOOP_RUNNING_OK is the flag for reconnecting or not"""
    sleep_time = 10
    while True:
        # check if we should reconnect at all
        if not CENSUS_LOOP_RUNNING_OK:
            break
        await connect_census()
        # if we are here, then census connection is closed/dropped
        print(f'failed to connect to census, waiting for {sleep_time} seconds')
        await asyncio.sleep(sleep_time)
        # waiting before the reconnec attempt
        if sleep_time < 5000:
            sleep_time = sleep_time * 2

def global_values_to_default() -> None:
    """When a character logged off or census connection was lot,
    all changed global values should get the default state and
    the algoritm begins to work from the scratch"""

    global KILLS, CURRENT_GUN, CHAR_ONLINE, CURRENT_GUN_ID, CHAR_ONLINE, LEADERS, CLOSEST_KILLS, GOAL_KILLS
    LEADERS = []
    KILLS = CURRENT_GUN = 'n/a' # n/a because this will be shown on the screen
    CURRENT_GUN_ID = CHAR_ONLINE = CLOSEST_KILLS = GOAL_KILLS = None


# FOR HOTKEYS AND BUTTONS

class Hotkey:
    """Hotkey class for OBS"""

    def __init__(self, callback, obs_settings, _id):
        self.obs_data = obs_settings
        self.hotkey_id = obs.OBS_INVALID_HOTKEY_ID
        self.hotkey_saved_key = None
        self.callback = callback
        self._id = _id

        self.load_hotkey()
        self.register_hotkey()
        self.save_hotkey()

    def register_hotkey(self):
        description = "Htk " + str(self._id)
        self.hotkey_id = obs.obs_hotkey_register_frontend(
            "htk_id" + str(self._id), description, self.callback
        )
        # loads into OBS the key combination array data for the given hotkey id
        obs.obs_hotkey_load(self.hotkey_id, self.hotkey_saved_key)

    def load_hotkey(self):
        # a key combination is represented by an array containing the main key plus
        # optional modifiers such as "shift" or "control". Such an array is stored
        # within a data settings object, is created or read if already existing
        # using obs_data_get_array (released by obs_data_array_release)
        # and is stored with obs_data_set_array
        self.hotkey_saved_key = obs.obs_data_get_array(
            self.obs_data, "htk_id" + str(self._id)
        )
        obs.obs_data_array_release(self.hotkey_saved_key)

    def save_hotkey(self):
        # obs_hotkey_save returns the key combination array set in OBS for a given hotkey id
        self.hotkey_saved_key = obs.obs_hotkey_save(self.hotkey_id)
        obs.obs_data_set_array(
            self.obs_data, "htk_id" + str(self._id), self.hotkey_saved_key
        )
        obs.obs_data_array_release(self.hotkey_saved_key)


class h:
    htk_copy = None  # this attribute will hold instance of Hotkey


h1 = h() # Hotkey to start to receive census events
h2 = h() # Hotkey to stop to receive census events
h3 = h() # Hotkey to show and hide the leaderboard


def start() -> str:
    """Used to start the execution. Called from a hotkey or a button"""

    global _THREAD, CENSUS_LOOP_RUNNING_OK
    if not CENSUS_LOOP_RUNNING_OK:
        # execution start with a background thread
        if not _THREAD and not _LOOP:
            CENSUS_LOOP_RUNNING_OK = True
            _THREAD = threading.Thread(None, run_stuff, daemon=True)
            _THREAD.start()
        return '<start> kills counter begun its work'
    else:
        return '<start> thread is already running, stop it first'


def start_hotkey(pressed):
    """Hotkey callback to starts/restart the whole algorithm"""

    if pressed:
        return print(start())


def stop_execution(pressed):
    """Hotkey callback to stop the algorithm"""

    if pressed:
        if _THREAD or _LOOP:
            script_unload()
            update_text(SOURCE, string_prepare())
            return print('Kills counter stops')
        else:
            return print('Kills counter isnt working')

def show_leaderboard_hotkey(pressed):
    """Hotkey callback to show the leaderboard on key pressed
    and hide on key release"""

    if pressed:
        # show the whole leaderboard. Grows down
        if len(LEADERS) > 1:
            update_text(SOURCE, 'LEADRERBOARD\n' + '\n'.join([ f'{leader[0]} - {leader[1]} kills' for leader in LEADERS ]))
        elif len(LEADERS) == 1:
            update_text(SOURCE, f'The leader is {CHAR_ONLINE}')
        else:
            update_text(SOURCE, f'The leaderboard was not loaded')
        return print('Showing leaders')
    else:
        # show the normal string
        update_text(SOURCE, string_prepare())
        return print('Not showing leaders')

# FOR ONSCREEN TEXT

def string_prepare():
    """contains a template of a commonly used string"""

    return f'{CURRENT_GUN} kills: {KILLS}{"/" + str(GOAL_KILLS) if GOAL_KILLS else ""}'

def update_text(text_source: str, scripted_text: str):
    """takes scripted_text , sets its value in obs on the screen"""

    with source_ar(text_source) as source, data_ar() as settings:
        obs.obs_data_set_string(settings, "text", scripted_text)
        obs.obs_source_update(source, settings)


def text_source_searcher() -> list:
    """Searches only text sources among all sources, returns
    a list of their names"""

    sources = obs.obs_enum_sources()
    result = []
    for source in sources:
        source_id = obs.obs_source_get_unversioned_id(source)
        if source_id == "text_gdiplus" or source_id == "text_ft2_source":
            result.append(obs.obs_source_get_name(source))
    obs.source_list_release(sources)    
    return result


# FOR LOOP AND THREAD EXECUTION

def run_stuff():
    """The main function. Runs the program logic"""
    global _LOOP
    if not SOURCE:
        print('<run_stuff> you forgot to assign the source!')
        return
    if not CENSUS_LOOP_RUNNING_OK:
        print(f'<run_stuff> LOOP is not allowed to run')
        return
    _LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_LOOP)
    print('<run_stuff> creating tasks')
    _LOOP.create_task(census_loop())
    print('<run_stuff> running')
    _LOOP.run_forever()
    # Stop anything that is running on the loop before closing. Most likely
    # using the loop run_until_complete function
    _LOOP.close()
    _LOOP = None


# SPECIAL FUNCTIONS WHICH ARE CALLED BY OBS

def script_description():
    """Shows text in OBS window. Works with html tags too"""

    return "<h2>PS2 special guns kills counter</h2> \n <p>The lord should be defeated</p> "


def script_update(settings):
    """Called every time when a user changes anything in hand-made settings"""

    global SOURCE
    # user can make a pick only among existing sources
    SOURCE = obs.obs_data_get_string(settings, "source")
    # but in a case we took the preserved source from the previous launch
    # and the source was deleted afterwards, we should check it out
    sources = text_source_searcher()
    if not SOURCE in sources:
        SOURCE = None


def script_properties():
    """Settings received via hand-made GUI"""

    props = obs.obs_properties_create() # creating the object
    # adding just one property in there - the list of sources
    p = obs.obs_properties_add_list(
        props,
        "source",
        "<h2>Text Source</h2>",
        obs.OBS_COMBO_TYPE_EDITABLE,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    obs.obs_property_set_long_description(p, "Add a text source in sources, put it's name here")
    # put all text sources in the drop box
    for name in text_source_searcher():
        obs.obs_property_list_add_string(p, name, name)

    obs.obs_properties_add_button(
        props, "button1", "Start", lambda *props: start()
    )

    obs.obs_properties_add_button(
        props, "button2", "Stop", lambda *props: script_unload()
    )

    return props


def script_save(settings):
    """Called when closing OBS"""
    # saving our binded hotkeys
    h1.htk_copy.save_hotkey()
    h2.htk_copy.save_hotkey()
    h3.htk_copy.save_hotkey()


def script_load(settings):
    """Called on OBS launch"""
    # addition of hotkeys to settings menu
    h1.htk_copy = Hotkey(start_hotkey, settings, "Start kills counter")
    h2.htk_copy = Hotkey(stop_execution, settings, "Stop kills counter")
    h3.htk_copy = Hotkey(show_leaderboard_hotkey, settings, "Show leaderboard")


def script_unload():
    """Called on reload. Tries to stop the loop and the deamonized thread."""

    global _THREAD, _LOOP, CENSUS_LOOP_RUNNING_OK
    CENSUS_LOOP_RUNNING_OK = False
    global_values_to_default()
    if _LOOP is not None:
        _LOOP.call_soon_threadsafe(lambda l: l.stop(), _LOOP)

    if _THREAD is not None:
        # Wait for 5 seconds, if it doesn't exit just move on not to block
        # OBS main thread. Logging something about the failure to properly exit
        # is advised.
        _THREAD.join(timeout=2)
        print('<script_unload> stopped the execution')
        _THREAD = None
