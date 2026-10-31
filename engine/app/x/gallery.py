"""Stream X post metadata to Vydlora; Void still downloads the media."""
from __future__ import annotations

def install():
    from gallery_dl import job
    from gallery_dl.extractor.twitter import TwitterExtractor
    transform = TwitterExtractor._transform_tweet
    directory = job.DataJob.handle_directory
    media = job.DataJob.handle_url
    run = job.DataJob.run

    def with_status(self, tweet):
        data = transform(self, tweet)
        status = dict(tweet.get("legacy") or tweet)
        user = data.get("user") or {}
        status["id_str"] = str(data["tweet_id"])
        status["user"] = {"screen_name": user.get("name"), "name": user.get("nick"),
                          "id_str": str(user.get("id") or ""),
                          "profile_image_url_https": user.get("profile_image")}
        data["_lora1_status"] = status
        return data

    def stream_directory(self, data):
        if self.jsonl:
            # Each directory is a complete post, including every media variant.
            # Avoid retaining the whole history or repeating it per attachment.
            self.out((2, "", self.filter(data)))
        else:
            directory(self, data)

    def stream_media(self, url, data):
        if not self.jsonl:
            media(self, url, data)

    def checked_run(self):
        result = run(self)
        if self.jsonl and self.exception:
            self.out((-1, {"error": type(self.exception).__name__, "message": str(self.exception)}))
            return 1
        return result

    TwitterExtractor._transform_tweet = with_status
    job.DataJob.handle_directory = stream_directory
    job.DataJob.handle_url = stream_media
    job.DataJob.run = checked_run

def main():
    install()
    from gallery_dl import main as gallery_main
    gallery_main()

if __name__ == "__main__":
    main()
