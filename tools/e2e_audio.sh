#!/usr/bin/env bash
# Build the e2e test recordings, spoken by Deepgram Aura (same key as
# transcription), with voices chosen because nova-3 diarizes them apart:
#
#   meeting.wav/.mp3   three voices introduce themselves, then one asks Daniel a question
#   oneonone.wav       Daniel introduces himself; Sarah asks two questions and never says
#                      her name, so only the meeting's roster can name her
#   farend.mp3         Sarah's half of that 1:1 alone, as a call tab would carry it
#
#   tools/e2e_audio.sh <out-dir>
#
# Reads DEEPGRAM_API_KEY from the environment or ~/.config/meeting-copilot/config.env.
set -euo pipefail

out="${1:?usage: tools/e2e_audio.sh <out-dir>}"
mkdir -p "$out"
cd "$out"
if [ -s meeting.wav ] && [ -s meeting.mp3 ] && [ -s oneonone.wav ] && [ -s farend.mp3 ]; then exit 0; fi

if [ -z "${DEEPGRAM_API_KEY:-}" ] && [ -f ~/.config/meeting-copilot/config.env ]; then
  DEEPGRAM_API_KEY=$(sed -n 's/^DEEPGRAM_API_KEY=//p' ~/.config/meeting-copilot/config.env)
fi
: "${DEEPGRAM_API_KEY:?DEEPGRAM_API_KEY is not set}"

say() {  # file voice text
  local body
  body=$(python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1]}))' "$3")
  curl -sS -f -X POST \
    "https://api.deepgram.com/v1/speak?model=$2&encoding=linear16&container=wav&sample_rate=16000" \
    -H "Authorization: Token $DEEPGRAM_API_KEY" -H "Content-Type: application/json" \
    --data "$body" -o "$1"
}

say 01.wav aura-asteria-en "Hi everyone, I'm Sarah, I lead the platform team. Thanks for making time today."
say 02.wav aura-orion-en "Hey, this is Marcus from the design team. Good to meet you."
say 03.wav aura-helios-en "Hi, I'm Daniel. I built the migration service we are going to talk about."
say 04.wav aura-asteria-en "Great. Daniel, how do you handle schema migrations in production without downtime?"

ffmpeg -loglevel error -y -f lavfi -i anullsrc=r=16000:cl=mono -t 1.2 -c:a pcm_s16le gap.wav
printf "file '%s'\n" 01.wav gap.wav 02.wav gap.wav 03.wav gap.wav 04.wav gap.wav gap.wav > list.txt
ffmpeg -loglevel error -y -f concat -safe 0 -i list.txt -ar 16000 -ac 1 -c:a pcm_s16le meeting.wav
ffmpeg -loglevel error -y -i meeting.wav -c:a libmp3lame -q:a 4 meeting.mp3

say 11.wav aura-helios-en "Hi, thanks for having me. I'm Daniel."
say 12.wav aura-asteria-en "Great to have you. So, how do you handle schema migrations in production without downtime?"
say 13.wav aura-asteria-en "And how would you roll that back if the backfill fails halfway?"
join() {  # out, parts...
  local out=$1; shift
  printf "file '%s'\n" "$@" > list.txt
  ffmpeg -loglevel error -y -f concat -safe 0 -i list.txt -ar 16000 -ac 1 -c:a pcm_s16le "$out"
}
join oneonone.wav 11.wav gap.wav 12.wav gap.wav 13.wav gap.wav gap.wav
join farend.wav 12.wav gap.wav 13.wav gap.wav gap.wav
ffmpeg -loglevel error -y -i farend.wav -c:a libmp3lame -q:a 4 farend.mp3
rm -f 0?.wav 1?.wav gap.wav list.txt farend.wav
