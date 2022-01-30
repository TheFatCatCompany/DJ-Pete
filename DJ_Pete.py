import asyncio
import functools
import itertools
import random
import math
import os
import discord
import youtube_dl
from async_timeout import timeout
from discord.ext import commands, tasks
import json

youtube_dl.utils.bug_reports_message = lambda: ''

async def userCheck(ctx):
    if ctx.author.id == 468900330244145153:
        await ctx.send("I'm not going to talk to you, you dirty mongrel.")
        return False
    return True

class VoiceError(Exception):
    pass

class YTDLError(Exception):
    pass

class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: commands.Context, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info, search, download=False, process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries']:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        webpage_url = process_info['webpage_url']
        partial = functools.partial(cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError('Couldn\'t fetch `{}`'.format(webpage_url))

        if 'entries' not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise YTDLError('Couldn\'t retrieve any matches for `{}`'.format(webpage_url))

        return cls(ctx, discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS), data=info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append('{} days'.format(days))
        if hours > 0:
            duration.append('{} hours'.format(hours))
        if minutes > 0:
            duration.append('{} minutes'.format(minutes))
        if seconds > 0:
            duration.append('{} seconds'.format(seconds))

        return ', '.join(duration)


class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(title='Now playing',
                               description='```css\n{0.source.title}\n```'.format(self),
                               color=discord.Color.blurple())
                 .add_field(name='Duration', value=self.source.duration)
                 .add_field(name='Requested by', value=self.requester.mention)
                 .add_field(name='Uploader', value='[{0.source.uploader}]({0.source.uploader_url})'.format(self))
                 .add_field(name='URL', value='[Click]({0.source.url})'.format(self))
                 .set_thumbnail(url=self.source.thumbnail))

        return embed


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.5
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop:
                try:
                    async with timeout(180):
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    return

            self.current.source.volume = self._volume
            try:
                self.voice.play(self.current.source, after=self.play_next_song)
            except:
                return
            await self.current.source.channel.send(embed=self.current.create_embed())

            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}
        self.mood = None
        self.known_users = None
        if os.path.exists("known_users.txt") and os.path.exists("mood.txt"):
            with open("known_users.txt", "r") as outfile:
                self.known_users = json.load(outfile)
            with open("mood.txt", "r") as outfile:
                self.mood = json.load(outfile)
        else:
            self.known_users = {}
            self.mood = 50
        self.saveIter = 0
        self.saveState.start()
        self.moodChange.start()
        bot.remove_command('help')
        
        

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage('This command can\'t be used in DM channels.')

        return True

    async def cog_before_invoke(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.send('I don\'t know what the hell you\'re trying to do: {}'.format(str(error)))
    
    

    async def change_opinion(self, id, num):
        if id in self.known_users:
            self.known_users[id] += num
     
    @commands.command(name='join', invoke_without_subcommand=True, aliases=['come'])
    @commands.check(userCheck)
    async def _join(self, ctx: commands.Context):
        await self.setup(ctx)
        if((self.mood * 0.25 + self.known_users[ctx.author.id] * .75) < 30):
            talknum = random.randint(0,2)
            self.mood += -0.2
            if talknum == 0:
                return await ctx.send("How about I don't join?")
            if talknum == 1:
                return await ctx.send("I'll just ignore that one.")
            if talknum == 2:
                return await ctx.send("No.")
        elif((self.mood * 0.25 + self.known_users[ctx.author.id] * .75) > 75):
            talknum = random.randint(0,2)
            self.mood += 0.2
            if talknum == 0:
                return await ctx.send("No problem!")
            if talknum == 1:
                return await ctx.send("Of course I'll join you guys!")
            if talknum == 2:
                return await ctx.send("Alright!")

        self.mood += 0.1
        
        destination = None
        try:
            destination = ctx.author.voice.channel
        except AttributeError:
            return
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return
            
        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='summon')
    @commands.has_permissions(manage_guild=True)
    @commands.check(userCheck)
    async def _summon(self, ctx: commands.Context, *, channel: discord.VoiceChannel = None):
        await self.setup(ctx)
        if((self.mood * 0.25 + self.known_users[ctx.author.id] * .75) < 10):
            talknum = random.randint(0,2)
            self.mood += -0.2
            await self.change_opinion(ctx.author.id, -3)
            if talknum == 0:
                return await ctx.send("I'm not gonna come.")
            if talknum == 1:
                return await ctx.send("I don't care if you're an admin, I'm not coming.")
            if talknum == 2:
                return await ctx.send("No. Screw. Off.")
        if not channel and not ctx.author.voice:
            raise VoiceError('You are neither connected to a voice channel nor specified a channel to join.')

        destination = channel or ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            await ctx.send('I have been summoned. Now what?')
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='leave', aliases=['disconnect'])
    @commands.has_permissions(manage_guild=True)
    @commands.check(userCheck)
    async def _leave(self, ctx: commands.Context):
        await self.setup(ctx)

        if not ctx.voice_state.voice:
            await self.change_opinion(ctx.author.id, -3)
            return await ctx.send('I\'m not in a voice channel right now. There\'s nowhere to leave.')
        
        if((self.mood * 0.25 + self.known_users[ctx.author.id] * .75) < 10):
            talknum = random.randint(0,2)
            self.mood += 0.1
            if talknum == 0:
                return await ctx.send("I'm not gonna leave.")
            if talknum == 1:
                return await ctx.send("You're an admin? Guess what, I don't care. I'm not leaving.")
            if talknum == 2:
                return await ctx.send("No. 😈")
        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]

    @commands.command(name='volume', aliases=['change volume'])
    @commands.check(userCheck)
    async def _volume(self, ctx: commands.Context, *, volume: int):
        await self.setup(ctx)
        if((self.mood * 0.25 + self.known_users[ctx.author.id] * .75) < 10):
            talknum = random.randint(0,2)
            self.mood += 0.1
            if talknum == 0:
                return await ctx.send("I'm not listening to you.")
            if talknum == 1:
                await ctx.send("You know what? Screw you. I'm setting volume to 0.")
                ctx.voice_state.volume = 0 / 100
                return await ctx.send('Volume set to {}%'.format(0))
            if talknum == 2:
                return await ctx.send("No. 😈")

        if not ctx.voice_state.is_playing:
            talknum = random.randint(0,2)
            self.mood += -0.1
            await self.change_opinion(ctx.author.id, -1)
            if talknum == 0:
                return await ctx.send("Volume is permanently 0... meaning that there's no song playing.")
            if talknum == 1:
                return await ctx.send("There is no song to change the volume of.")
            if talknum == 2:
                return await ctx.send("Can't change the volume of a song that isn't playing.")

        if 0 > volume > 100:
            return await ctx.send('Volume must be between 0 and 100.')
            self.mood += -0.1

        ctx.voice_state.volume = volume / 100
        await ctx.send('Volume set to {}%'.format(volume))
        if volume == 100:
            talknum = random.randint(0,2)
            self.mood += -0.5
            await self.change_opinion(ctx.author.id, -5)
            if talknum == 0:
                return await ctx.send("Are you trying to make me lose my hearing?")
            if talknum == 1:
                return await ctx.send("That's so loud! I'm gonna cover my ears.")
            if talknum == 2:
                return await ctx.send("You must love getting your eardrums blown out. Me? I'm getting earplugs.")
            
        elif volume == 99:

            return await ctx.send("But still, volume 99? Really? That's kind of weird.")
        elif volume == 69:
            if((self.mood * 0.25 + self.known_users[ctx.author.id] * .75) > 75):
                talknum = random.randint(0,2)
                if talknum == 0:
                    return await ctx.send("*Sigh*- not a very funny joke.")
                if talknum == 1:
                    return await ctx.send("Bruh. I know what you did.")
                if talknum == 2:
                    return await ctx.send("God damn it.")
                self.mood += -1
            await self.change_opinion(ctx.author.id, -10)
            talknum = random.randint(0,2)
            if talknum == 0:
                return await ctx.send("Are you trying to make fun of me?")
            if talknum == 1:
                return await ctx.send("wOaH, 69!1!!1!!11!! hOw HiLaRioUS!1!1!!111!")
            if talknum == 2:
                return await ctx.send("Wow. I'm. Laughing. So. Hard. Right. Now. What. An. Original. And. Funny. Joke.")
        elif volume == 50:
            await self.change_opinion(ctx.author.id, 5)
            self.mood += 1
            return await ctx.send("Right in the middle. Right where I like it.")
            
        elif volume == 0:
            self.mood += -2
            await self.change_opinion(id, -10)
            return await ctx.send("What's the point of playing music if nobody hears it? Why do I exist if nobody can hear the music? You make me sad, " + ctx.author.mention + ". Yes, I'm calling you out. You deserve it. Screw you.")

    @commands.command(name='what is the song now', aliases=['current song', 'what is playing'])
    @commands.check(userCheck)
    async def _now(self, ctx: commands.Context):
        await self.setup(ctx)

        await ctx.send(embed=ctx.voice_state.current.create_embed())

    @commands.command(name='pause')
    @commands.has_permissions(manage_guild = True)
    @commands.check(userCheck)
    async def _pause(self, ctx: commands.Context):
        await self.setup(ctx)
        if ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction('⏯')
            
    @_pause.error
    async def pause_error(error, ctx):
        if isinstance(error, MissingPermissions):
            await ctx.send(ctx.author.mention + ", I won't pause the song. Deal with it.")

    @commands.command(name='resume')
    @commands.has_permissions(manage_guild = True)
    @commands.check(userCheck)
    async def _resume(self, ctx: commands.Context):
        await self.setup(ctx)
        if ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction('⏯')
    @_resume.error
    async def resume_error(error, ctx):
        if isinstance(error, MissingPermissions):
            await ctx.send(ctx.author.mention + ", shut up. I'm not going to play the song.")

    @commands.command(name='stop')
    @commands.has_permissions(manage_guild = True)
    @commands.check(userCheck)
    async def _stop(self, ctx: commands.Context):
        await self.setup(ctx)

        ctx.voice_state.songs.clear()
        ctx.voice_state.voice.stop()
        await ctx.message.add_reaction('⏹')
    @_stop.error
    async def stop_error(error, ctx):
        if isinstance(error, MissingPermissions):
            await ctx.send(ctx.author.mention + ", you're not somebody I was told to listen to. Therefore, the song will continue. Also screw you.")

    @commands.command(name='skip')
    @commands.check(userCheck)
    async def _skip(self, ctx: commands.Context):
        await self.setup(ctx)

        if not ctx.voice_state.is_playing:
            return await ctx.send('Skip what? Skip stones? Classes? Are you telling me to skip across the room? Because there are no songs to skip.')
            self.mood -= 1
            await self.change_opinion(ctx.author.id, -1)
        voter = ctx.message.author
        if voter == ctx.voice_state.current.requester:
            await ctx.message.add_reaction('⏭')
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >= 3:
                await ctx.message.add_reaction('⏭')
                ctx.voice_state.skip()
            else:
                await ctx.send("Added a vote to skip, currently at **{}/3**".format(total_votes))

        else:
            await ctx.send('You have already voted to skip this song.')

    @commands.command(name='force skip', aliases=['fs'])
    @commands.has_permissions(manage_guild = True)
    @commands.check(userCheck)
    async def force_skip(self, ctx: commands.Context):
         await self.setup(ctx)

         if not ctx.voice_state.is_playing:
            return await ctx.send('Skip what? Skip stones? Classes? Are you telling me to skip across the room? Because there are no songs to skip.')
         await ctx.message.add_reaction('⏭')
         ctx.voice_state.skip()
        


    @commands.command(name='queue')
    @commands.check(userCheck)
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        await self.setup(ctx)
        if len(ctx.voice_state.songs) == 0:
            return await ctx.send("Nothing in the queue, dude. Tell me to play something and I'll add it.")

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)

        embed = (discord.Embed(description='**{} tracks:**\n\n{}'.format(len(ctx.voice_state.songs), queue))
                 .set_footer(text='Viewing page {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    @commands.command(name='shuffle')
    @commands.check(userCheck)
    async def _shuffle(self, ctx: commands.Context):
        await self.setup(ctx)

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send("You're shuffling... nothing. Idiot.")
            self.mood -= -0.2
            await self.change_opinion(ctx.author.id, -2)
        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction("Alright, I'll shuffle the queue. Don't blame me if something you don't like comes first.")

    @commands.command(name='remove')
    @commands.check(userCheck)
    async def _remove(self, ctx: commands.Context, index: int):
        await self.setup(ctx)

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send("There's literally nothing to take out.")
        if index > len(ctx.voice_state.songs):
            return await ctx.send("You can't remove something that's not there.")
        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction("")

    @commands.command(name='loop')
    @commands.check(userCheck)
    async def _loop(self, ctx: commands.Context):
        await self.setup(ctx)

        if not ctx.voice_state.is_playing:
            if not ctx.author.voice or not ctx.author.voice.channel:
                return await ctx.send("You're not even in a voice channel, idiot.")
            await ctx.send("You're looping... nothing. Enjoy The Sounds Of Silence, I guess, hehehehehe.")
            return await ctx.invoke(self.bot.get_command('play'), search= "Simon & Garfunkel - The Sounds of Silence (Audio)")
            
        
        ctx.voice_state.loop = not ctx.voice_state.loop
        await ctx.message.add_reaction('You want to hear this song *again?* Fine.')

    @commands.command(name='play')
    @commands.check(userCheck)
    async def _play(self, ctx: commands.Context, *, search: str):
        await self.setup(ctx)
        if((self.mood * 0.25 + self.known_users[ctx.author.id] * .75) < 0):
            await self.change_opinion(ctx.author.id, -5)
            self.mood -= 5
            return await ctx.send("Fuck. Off.")

        elif((self.mood * 0.25 + self.known_users[ctx.author.id] * .75) < 10):
            await self.change_opinion(ctx.author.id, -5)
            self.mood -= 2
            return await ctx.send("Leave me alone.")
        

        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)
        if not ctx.author.voice or not ctx.author.voice.channel:
            return
        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
            except YTDLError as e:
                await ctx.send('My brain broke. Please help: {}'.format(str(e)))
            else:
                song = Song(source)

                await ctx.voice_state.songs.put(song)
                if((self.mood * 0.25 + self.known_users[ctx.author.id] * .75) < 20):
                    await ctx.send("Here's your damn thing. {}, right? Now stop bothering me.".format(str(source)))
                    self.mood += -1
                    await self.change_opinion(ctx.author.id, -5)
                elif(self.mood * 0.25 + self.known_users[ctx.author.id] * .75) < 30:
                    await ctx.send("I put {} in the queue. I should seriously be paid for this.".format(str(source)))
                    self.mood += 0.5
                elif((self.mood * 0.25 + self.known_users[ctx.author.id] * .75) < 40):
                    await ctx.send("Here, {}. Have fun.".format(str(source)))
                    self.mood += 1
                    await self.change_opinion(ctx.author.id, 1)
                elif((self.mood * 0.25 + self.known_users[ctx.author.id] * .75) < 50):
                    await ctx.send("I put {} in the queue.".format(str(source)))
                    self.mood += 1
                    await self.change_opinion(ctx.author.id, 1)
                elif((self.mood * 0.25 + self.known_users[ctx.author.id] * .75) < 60):
                    await ctx.send("Alright, I got {} in the queue. Anything else?".format(str(source)))
                    self.mood += 1.5
                    await self.change_opinion(ctx.author.id, 2)
                elif((self.mood * 0.25 + self.known_users[ctx.author.id] * .75) < 70):
                    await ctx.send("{} is in. Whatever you need me for, I'm here.".format(str(source)))
                    self.mood += 2
                    await self.change_opinion(ctx.author.id, 2.5)
                elif((self.mood * 0.25 + self.known_users[ctx.author.id] * .75) < 80):
                    await ctx.send("{}, in the queue! I'm always happy to help.".format(str(source)))
                    self.mood += 2
                    await self.change_opinion(ctx.author.id, 3)
                elif((self.mood * 0.25 + self.known_users[ctx.author.id] * .75) < 90):
                    await ctx.send("{} is in! If there's anything else you need me for, I'm available!".format(str(source)))
                    self.mood += 2
                    await self.change_opinion(ctx.author.id, 3)
                else:
                    await ctx.send("We got {} in the queue! I love this job.".format(str(source)))
                    self.mood += 2
                    await self.change_opinion(ctx.author.id, 3)
    @commands.command(name = 'feelings')
    @commands.check(userCheck)
    async def _mood(self, ctx: commands.Context):
        await self.setup(ctx)
        if(self.mood  < 0):
            self.mood += 5
            await self.change_opinion(ctx.author.id, 10)
            return await ctx.send("I feel like utter shit. Utter, utter shit... sorry about that. Thanks for worrying about me.")
            
        elif(self.mood < 10):
            self.mood += 2
            await self.change_opinion(ctx.author.id, 3)
            return await ctx.send("Not great. Like really not good. Thanks for asking. Seriously.")
        elif(self.mood < 20):
            self.mood += 1
            await self.change_opinion(ctx.author.id, 1)
            await ctx.send("Not amazing. Thanks for asking.")            
        elif(self.mood < 30):
            self.mood += 1
            await self.change_opinion(ctx.author.id, 0.5)
            await ctx.send("I could be feeling better, I guess. Thanks for asking.")
           
        elif(self.mood < 40):
            await ctx.send("I'm doing okay, sorta.")
            
        elif(self.mood < 50):
            await ctx.send("I'm feeling fine.")
           
        elif(self.mood < 60):
            await ctx.send("I'm doing pretty good, I think.")            
        elif(self.mood < 70):
            await ctx.send("I'm feeling pretty good.")
           
        elif(self.mood < 80):
            await ctx.send("I'm pretty happy right now.")
            
        elif(self.mood < 90):
            await ctx.send("I don't think I've been happier in a very long time.")
            
        else:
            await ctx.send("I think I'm the happiest I can be at this point.")
    
    @commands.command(name = 'opinion')
    @commands.check(userCheck)
    async def _opinion(self, ctx: commands.Context):
        await self.setup(ctx)
        if(self.known_users[ctx.author.id]  < 0):
            return await ctx.send("Fuck you. I fucking hate you.")
            
        elif(self.known_users[ctx.author.id] < 10):
            self.mood += 2
            await self.change_opinion(ctx.author.id, 3)
            return await ctx.send("I honestly kind of dislike you. A lot.")
        elif(self.known_users[ctx.author.id] < 20):
            self.mood += 1
            await self.change_opinion(ctx.author.id, 1)
            await ctx.send("To be honest, I don't think I like you all that much.")            
        elif(self.known_users[ctx.author.id] < 30):
            self.mood += 1
            await self.change_opinion(ctx.author.id, 0.5)
            await ctx.send("You're a little annoying.")
           
        elif(self.known_users[ctx.author.id] < 40):
            await ctx.send("You're... alright, maybe?")
            
        elif(self.known_users[ctx.author.id] < 50):
            await ctx.send("I don't dislike you, persay.")
           
        elif(self.known_users[ctx.author.id] < 60):
            await ctx.send("I think you're a pretty alright person.")            
        elif(self.known_users[ctx.author.id] < 70):
            await ctx.send("We're friends, right?")
           
        elif(self.known_users[ctx.author.id] < 80):
            await ctx.send("C'mon, we're friends!")
            
        elif(self.known_users[ctx.author.id] < 90):
            await ctx.send("We're good friends! You know that!")
            
        else:
            await ctx.send("You're the nicest person I've ever met! 💕")
    @commands.command(name = 'help')
    @commands.check(userCheck)
    async def _help(self, ctx: commands.Context):
        embed = discord.Embed(title = "What Pete Will Listen To", description = "Prefix = Pete, ", colour = discord.Colour.blurple()) 
        embed.add_field(name = "Pete, play x", value = "Plays x in the voice channel you're in.", inline = True)
        embed.add_field(name = "Pete, join", value = "Pete will join the voice channel you're in.", inline = True)
        embed.add_field(name = "Pete, summon x", value = "Summons Pete to the voice channel x. Admin only.", inline = True)
        embed.add_field(name = "Pete, leave", value = "Pete leaves the voice channel and clears the queue.", inline = True)
        embed.add_field(name = "Pete, pause", value = "Pete pauses the song. Admin only.", inline = True)
        embed.add_field(name = "Pete, resume", value = "Pete resumes the song. Admin only.", inline = True)
        embed.add_field(name = "Pete, stop", value = "Plays stops all songs and clears the queue.", inline = True)
        embed.add_field(name = "Pete, what is the song now", value = "Pete displays the currently playing song.", inline = True)
        embed.add_field(name = "Pete, queue", value = "Pete displays the current queue.", inline = True)
        embed.add_field(name = "Pete, shuffle", value = "Pete shuffles the queue.", inline = True)
        embed.add_field(name = "Pete, remove x", value = "Pete removes the song at index x in the queue.", inline = True)
        embed.add_field(name = "Pete, loop", value = "Pete loops the song.", inline = True)
        embed.add_field(name = "Pete, skip", value = "Pete starts a skip vote. 3 votes required to skip the song.", inline = True)
        embed.add_field(name = "Pete, force skip", value = "Pete skips the song. Admin only.", inline = True)
        embed.add_field(name = "Pete, volume x", value = "Pete changes the volume to x. x is 1-100.", inline = True)
        embed.add_field(name = "Pete, feelings", value = "Pete tells you his current mood.", inline = True)
        embed.add_field(name = "Pete, opinion", value = "Pete tells you how he feels about you.", inline = True)
        embed.add_field(name = "Pete, commands", value = "This command.", inline = True)
        await ctx.send(embed = embed)

    @tasks.loop(minutes = 30)
    async def moodChange(self):
        if self.mood <= 50:
            self.mood += 10
        elif self.mood > 50:
            self.mood -= 10
    
    @tasks.loop(seconds = 30)
    async def saveState(self):
        self.saveIter += 1
        with open("known_users.txt", "w") as outfile:
            json.dump(self.known_users, outfile)
        with open("mood.txt", "w") as outfile:
            json.dump(self.mood, outfile)
        print("Save Iteration: " + str(self.saveIter))



        

    @_join.before_invoke
    @_play.before_invoke
    async def ensure_voice_state(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("I can't find you since you're not in a voice channel. Stop bothering me.")
            return False
        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                await ctx.send('Bot is already in a voice channel.')
                return False
        return True

    async def setup(self, ctx: commands.Context):
        if ctx.author.id not in self.known_users.keys():
            self.known_users[ctx.author.id] = 50
            print("New user: " + str(ctx.author.id))


bot = commands.Bot('Pete, ', description='Is proud of his Sennheiser headphones. A little sassy.')
bot.add_cog(Music(bot))


@bot.event
async def on_ready():
    print('Logged in as:\n{0.user.name}\n{0.user.id}'.format(bot))
    await bot.change_presence(activity=discord.Game('music on Sennheisers. Type "Pete, help" for a command list.'))
        

bot.run('BOT ID')