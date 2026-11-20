<p align="center">
  <img src="./assets/savely_logo.jpg" alt="Savely logo" width="80" height="80">
</p>

<h1 align="center">Savely</h1>

<p align="center">Search the posts you save on Instagram, in plain language.</p>

<p align="center">
  <a href="docs/ARCHITECTURE.md">Architecture</a>
  ·
  <a href="docs/decisions/">Decisions</a>
  ·
  <a href="docs/development.md">Development</a>
</p>

You save posts on Instagram all the time: a restaurant to try, a movie to watch, a study tip. Instagram lets you sort them into collections, but you can't search or filter inside them. Standing in a new neighbourhood, you can't ask "which restaurants did I save around here?". So the saves pile up and get forgotten.

Savely is a chat agent for the things you save. You share a post to the Savely bot by direct message, as easily as tapping Save. Savely works out what the post is about and remembers it. Later you ask in plain language, in any language, and it answers from your own saves, with links.

Instagram comes first. TikTok, X and Facebook are planned through the same backend.

## What you get

Items marked *(coming)* are planned for Phase 1 and not built yet.

- **Works for any kind of post.** Every save gets an open category and tags in plain words, such as "restaurant", "car review" or "guitar lesson". Nothing is forced into a fixed list. Places, movies and series, and study material get extra details on top.
- **It listens to the video.** When Savely gets the video file, it turns what's said into text, so a reel that only says the restaurant's name out loud can still be found. Reels shared by DM arrive without their video, so for those Savely reads the caption.
- **Any language.** Saves and questions can be in any language. Search matches what you type to the words in your own saves, so "Kore mutfağı" can find a save tagged "Korean food".
- **Places in any country, with map links.** *(coming)* Each place is looked up on Google Maps, so you can search by city, district or neighbourhood, or near you.
- **Movies and series, with where they stream.** *(coming)* Each title is looked up on TMDB, so you can ask what's on Netflix in your country.
- **Official APIs only.** Savely never asks for your Instagram password and never logs in as you.
- **Delete your data any time.** "Delete my data" removes your saves, your messages and the media stored for you. *(coming: asking for it in the chat)*

## How it works

1. **Share.** Send a post or reel to the Savely bot in an Instagram DM. You can add a short note right after it, like "for my movies collection".
2. **Savely indexes it once.** It works out what the post is, listens to the video when it has the file, picks out the details (a name, an area, a cuisine, a subject), and replies with how it was saved. This happens once, when you save, so asking later is fast.
3. **Ask.** *(coming)* Ask in plain language, in any language:
   - "restaurants in Beşiktaş I saved"
   - "horror movies on Netflix from my Movies collection"
   - "Brooklyn'de kaydettiğim kafeler" (Turkish: "cafés I saved in Brooklyn")
   - "mis vídeos de matemáticas para el examen" (Spanish: "my maths videos for the exam")

## Wait, is this a scraper?

No. Savely only uses Instagram's official Messaging API. It never asks for your password, never logs in to your account, and never reads a post's web page. That's also why a reel shared by DM is read from its caption: the official API doesn't hand over its video.

It isn't a general chatbot either. It answers questions about your own saves. And it isn't social: nothing you save is shown to anyone else.

## Status

Phase 1, the Instagram DM version, is in progress. Savely isn't open to the public yet: it runs in Meta's development mode, for test accounts only.

Working today: the bot receives the posts and reels you share, indexes each one (its type, its details, the speech in the video when there is a file, an open category and tags), and confirms by DM how it was saved. The search behind the chat is built. Next come looking up places and movies on Google Maps and TMDB, and the chat agent that answers your questions.

## Documentation

- [Development](docs/development.md): set up, run and test Savely, and how each part works.
- [Architecture](docs/ARCHITECTURE.md): the design, and the source of truth.
- [Decision records](docs/decisions/): why things are the way they are.
