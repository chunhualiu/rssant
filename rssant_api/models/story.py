from django.utils import timezone
from django.db import connection, transaction
from html2text import HTML2Text
from cached_property import cached_property

from rssant_common.validator import StoryUnionId, FeedUnionId
from .helper import Model, ContentHashMixin, models, optional, User
from .feed import Feed, UserFeed
from .errors import FeedNotFoundError, StoryNotFoundError

MONTH_18 = timezone.timedelta(days=18 * 30)
ONE_MONTH = timezone.timedelta(days=30)


STORY_DETAIL_FEILDS = ['summary', 'content']
USER_STORY_DETAIL_FEILDS = ['story__summary', 'story__content']


def convert_summary(summary):
    h = HTML2Text()
    h.ignore_links = True
    return h.handle(summary or "")


FEED_STORY_PUBLISH_PERIOD_FIELDS = [
    'id',
    'total_storys',
    'story_publish_period',
    'offset_early_story',
    'dt_early_story_published',
    'dt_latest_story_published',
]


class Story(Model, ContentHashMixin):
    """故事"""

    class Meta:
        unique_together = (
            ('feed', 'offset'),
            ('feed', 'unique_id'),
        )
        indexes = [
            models.Index(fields=["feed", "offset"]),
            models.Index(fields=["feed", "dt_published"]),
            models.Index(fields=["feed", "unique_id"]),
        ]

    class Admin:
        display_fields = ['feed_id', 'offset', 'title', 'link']

    feed = models.ForeignKey(Feed, on_delete=models.CASCADE)
    offset = models.IntegerField(help_text="Story在Feed中的位置")
    unique_id = models.CharField(max_length=200, help_text="Unique ID")
    title = models.CharField(max_length=200, help_text="标题")
    link = models.TextField(help_text="文章链接")
    author = models.CharField(max_length=200, **optional, help_text='作者')
    dt_published = models.DateTimeField(help_text="发布时间")
    dt_updated = models.DateTimeField(**optional, help_text="更新时间")
    dt_created = models.DateTimeField(auto_now_add=True, help_text="创建时间")
    dt_synced = models.DateTimeField(**optional, help_text="最近一次同步时间")
    summary = models.TextField(**optional, help_text="摘要或较短的内容")
    content = models.TextField(**optional, help_text="文章内容")

    @staticmethod
    def get_by_offset(feed_id, offset, detail=False):
        q = Story.objects.filter(feed_id=feed_id, offset=offset)
        if not detail:
            q = q.defer(*STORY_DETAIL_FEILDS)
        return q.get()

    @staticmethod
    def bulk_save_by_feed(feed_id, storys, batch_size=100):
        if not storys:
            return 0, 0  # num_modified, num_reallocate
        # 先排序，分配offset时保证offset和dt_published顺序一致
        storys = list(sorted(storys, key=lambda x: (x['dt_published'], x['unique_id'])))
        with transaction.atomic():
            feed = Feed.objects\
                .only('_version', *FEED_STORY_PUBLISH_PERIOD_FIELDS)\
                .get(pk=feed_id)
            offset = feed.total_storys
            unique_ids = [x['unique_id'] for x in storys]
            story_objects = {}
            q = Story.objects.filter(feed_id=feed_id, unique_id__in=unique_ids)
            for story in q.all():
                story_objects[story.unique_id] = story
            new_story_objects = []
            num_modified = 0
            now = timezone.now()
            for data in storys:
                unique_id = data['unique_id']
                content_hash_base64 = data['content_hash_base64']
                is_story_exist = unique_id in story_objects
                if is_story_exist:
                    story = story_objects[unique_id]
                    if not story.is_modified(content_hash_base64):
                        continue
                else:
                    story = Story(feed_id=feed_id, unique_id=unique_id, offset=offset)
                    story_objects[unique_id] = story
                    new_story_objects.append(story)
                    offset += 1
                story.content_hash_base64 = content_hash_base64
                story.content = data['content']
                story.summary = data['summary']
                story.title = data["title"]
                story.link = data["link"]
                story.author = data["author"]
                # 发布时间只第一次赋值，不更新
                if not story.dt_published:
                    story.dt_published = data['dt_published']
                story.dt_updated = data['dt_updated']
                story.dt_synced = now
                if is_story_exist:
                    story.save()
                num_modified += 1
            if new_story_objects:
                Story.objects.bulk_create(new_story_objects, batch_size=batch_size)
                early_dt_published = new_story_objects[0].dt_published
                num_reallocate = Story._reallocate_offset(feed.id, early_dt_published)
                Story._update_feed_story_publish_period(feed, total_storys=offset)
            else:
                num_reallocate = 0
            return num_modified, num_reallocate

    @staticmethod
    def _reallocate_offset(feed_id, early_dt_published=None):
        if early_dt_published:
            should_reallocate = timezone.now() - early_dt_published > ONE_MONTH
            if not should_reallocate:
                return 0
        early_story_offset = 0
        if early_dt_published:
            # 找出第一个比early_dt_published更早的story
            early_story = Story.objects\
                .only('id', 'offset')\
                .filter(feed_id=feed_id, dt_published__lt=early_dt_published)\
                .order_by('-dt_published')\
                .first()
            if early_story:
                early_story_offset = early_story.offset
        # 所有可能需要重排的story
        q = Story.objects.filter(feed_id=feed_id)\
            .only('_version', 'id', 'offset', 'dt_published', 'unique_id')\
            .filter(offset__gte=early_story_offset)\
            .order_by('dt_published', 'unique_id')
        storys = list(q.all())
        updates = []
        for offset, story in enumerate(storys):
            offset = offset + early_story_offset
            if story.offset != offset:
                # 需要重排，先将 offset 变负数，避免违反 (feed_id, offset) unique 约束
                story.offset = -offset - 1
                story.save()
                updates.append(story)
        for story in updates:
            story.offset = -(story.offset + 1)
            story.save()
        return len(updates)

    @staticmethod
    def reallocate_offset(feed_id, early_dt_published=None):
        with transaction.atomic():
            return Story._reallocate_offset(feed_id, early_dt_published=early_dt_published)

    @staticmethod
    def _update_feed_story_publish_period(feed, total_storys):
        if total_storys <= 0:
            return False  # is_updated
        latest_story = Story.objects\
            .only('id', 'offset', 'dt_published')\
            .get(feed_id=feed.id, offset=total_storys - 1)
        dt_latest_story_published = latest_story.dt_published
        dt_18_months_ago = dt_latest_story_published - MONTH_18
        # 找出第一个比dt_18_months_ago更晚的story
        early_story = Story.objects\
            .only('id', 'offset', 'dt_published')\
            .filter(feed_id=feed.id, dt_published__gte=dt_18_months_ago)\
            .order_by('dt_published')\
            .first()
        if not early_story:
            early_story = Story.objects\
                .only('id', 'offset', 'dt_published')\
                .filter(feed_id=feed.id, offset=0)\
                .get()
        offset_early_story = early_story.offset
        dt_early_story_published = early_story.dt_published
        dt_published_days = (dt_latest_story_published - dt_early_story_published).days
        num_published_storys = total_storys - offset_early_story
        assert num_published_storys > 0, 'num_published_storys <= 0 when compute story_publish_period!'
        story_publish_period = round(max(dt_published_days, 1) / num_published_storys)
        is_updated = (
            (feed.offset_early_story != offset_early_story) or
            (feed.total_storys != total_storys)
        )
        feed.total_storys = total_storys
        feed.offset_early_story = offset_early_story
        feed.dt_latest_story_published = dt_latest_story_published
        feed.dt_early_story_published = dt_early_story_published
        feed.story_publish_period = story_publish_period
        feed.save()
        return is_updated

    @staticmethod
    def update_feed_story_publish_period(feed_id):
        with transaction.atomic():
            feed = Feed.objects\
                .only('_version', *FEED_STORY_PUBLISH_PERIOD_FIELDS)\
                .get(pk=feed_id)
            return Story._update_feed_story_publish_period(
                feed, total_storys=feed.total_storys)

    @staticmethod
    def fix_feed_total_storys(feed_id):
        with transaction.atomic():
            feed = Feed.objects.only('_version', 'id', 'total_storys').get(pk=feed_id)
            total_storys = Story.objects.filter(feed_id=feed_id).count()
            if feed.total_storys != total_storys:
                feed.total_storys = total_storys
                feed.save()
                return True
            return False

    @staticmethod
    def query_feed_incorrect_total_storys():
        sql = """
        SELECT id, total_storys, correct_total_storys
        FROM rssant_api_feed AS current
        JOIN (
            SELECT feed_id, count(1) AS correct_total_storys
            FROM rssant_api_story
            GROUP BY feed_id
        ) AS correct
        ON current.id=correct.feed_id
        WHERE total_storys!=correct_total_storys
        """
        with connection.cursor() as cursor:
            cursor.execute(sql)
            return list(cursor.fetchall())


class UserStory(Model):
    class Meta:
        unique_together = [
            ('user', 'story'),
            ('user_feed', 'offset'),
            ('user', 'feed', 'offset'),
        ]
        indexes = [
            models.Index(fields=["user", "feed", "offset"]),
            models.Index(fields=["user", "feed", "story"]),
        ]

    class Admin:
        display_fields = ['user_id', 'feed_id', 'story_id', 'is_watched', 'is_favorited']
        search_fields = ['user_feed_id']

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    story = models.ForeignKey(Story, on_delete=models.CASCADE)
    feed = models.ForeignKey(Feed, on_delete=models.CASCADE)
    user_feed = models.ForeignKey(UserFeed, on_delete=models.CASCADE)
    offset = models.IntegerField(help_text="Story在Feed中的位置")
    dt_created = models.DateTimeField(auto_now_add=True, help_text="创建时间")
    is_watched = models.BooleanField(default=False)
    dt_watched = models.DateTimeField(**optional, help_text="关注时间")
    is_favorited = models.BooleanField(default=False)
    dt_favorited = models.DateTimeField(**optional, help_text="标星时间")

    @staticmethod
    def get_by_pk(pk, user_id=None, detail=False):
        q = UserStory.objects.select_related('story')
        if not detail:
            q = q.defer(*USER_STORY_DETAIL_FEILDS)
        if user_id is not None:
            q = q.filter(user_id=user_id)
        user_story = q.get(pk=pk)
        return user_story

    @staticmethod
    def get_by_offset(user_id, feed_id, offset, detail=False):
        q = UserStory.objects.select_related('story')
        q = q.filter(user_id=user_id, feed_id=feed_id, offset=offset)
        if not detail:
            q = q.defer(*USER_STORY_DETAIL_FEILDS)
        user_story = q.get()
        return user_story


class UnionStory:

    def __init__(self, story, *, user_id, user_feed_id, user_story=None, detail=False):
        self._story = story
        self._user_id = user_id
        self._user_feed_id = user_feed_id
        self._user_story = user_story
        self._detail = detail

    @cached_property
    def id(self):
        return StoryUnionId(self._user_id, self._story.feed_id, self._story.offset)

    @property
    def user_id(self):
        return self._user_id

    @cached_property
    def feed_id(self):
        return FeedUnionId(self._user_id, self._story.feed_id)

    @property
    def offset(self):
        return self._story.offset

    @property
    def unique_id(self):
        return self._story.unique_id

    @property
    def title(self):
        return self._story.title

    @property
    def link(self):
        return self._story.link

    @property
    def dt_published(self):
        return self._story.dt_published

    @property
    def dt_updated(self):
        return self._story.dt_updated

    @property
    def dt_created(self):
        return self._story.dt_created

    @property
    def dt_synced(self):
        return self._story.dt_synced

    @property
    def is_watched(self):
        if not self._user_story:
            return False
        return self._user_story.is_watched

    @property
    def dt_watched(self):
        if not self._user_story:
            return None
        return self._user_story.dt_watched

    @property
    def is_favorited(self):
        if not self._user_story:
            return False
        return self._user_story.is_favorited

    @property
    def dt_favorited(self):
        if not self._user_story:
            return None
        return self._user_story.dt_favorited

    @property
    def summary(self):
        return self._story.summary

    @property
    def content(self):
        return self._story.content

    def to_dict(self):
        ret = dict(
            id=self.id,
            user=dict(id=self.user_id),
            feed=dict(id=self.feed_id),
            offset=self.offset,
            unique_id=self.unique_id,
            title=self.title,
            link=self.link,
            dt_published=self.dt_published,
            dt_updated=self.dt_updated,
            dt_created=self.dt_created,
            dt_synced=self.dt_synced,
            is_watched=self.is_watched,
            dt_watched=self.dt_watched,
            is_favorited=self.is_favorited,
            dt_favorited=self.dt_favorited,
        )
        if self._detail:
            ret.update(
                summary=self.summary,
                content=self.content,
            )
        return ret

    @staticmethod
    def _check_user_feed_by_story_unionid(story_unionid):
        user_id, feed_id, offset = story_unionid
        q = UserFeed.objects.only('id').filter(user_id=user_id, feed_id=feed_id)
        try:
            user_feed = q.get()
        except UserFeed.DoesNotExist:
            raise StoryNotFoundError()
        return user_feed.id

    @staticmethod
    def get_by_id(story_unionid, detail=False):
        user_feed_id = UnionStory._check_story_unionid(story_unionid)
        user_id, feed_id, offset = story_unionid
        q = UserStory.objects.select_related('story')
        q = q.filter(user_id=user_id, feed_id=feed_id, offset=offset)
        if not detail:
            q = q.defer(*USER_STORY_DETAIL_FEILDS)
        try:
            user_story = q.get()
        except UserStory.DoesNotExist:
            user_story = None
            try:
                story = Story.get_by_offset(feed_id, offset, detail=detail)
            except Story.DoesNotExist:
                raise StoryNotFoundError()
        else:
            story = user_story.story
        return UnionStory(
            story,
            user_id=user_id,
            user_feed_id=user_feed_id,
            user_story=user_story,
            detail=detail
        )

    @staticmethod
    def get_by_feed_offset(feed_unionid, offset, detail=False):
        story_unionid = StoryUnionId(*feed_unionid, offset)
        return UnionStory.get_by_id(story_unionid)

    @staticmethod
    def _merge_storys(storys, user_storys, *, user_id, user_feeds=None, detail=False):
        user_storys_map = {x.story_id: x for x in user_storys}
        if user_feeds:
            user_feeds_map = {x.feed_id: x.id for x in user_feeds}
        else:
            user_feeds_map = {x.feed_id: x.user_feed_id for x in user_storys}
        ret = []
        for story in storys:
            user_story = user_storys_map.get(story.id)
            user_feed_id = user_feeds_map.get(story.feed_id)
            ret.append(UnionStory(
                story,
                user_id=user_id,
                user_feed_id=user_feed_id,
                user_story=user_story,
                detail=detail
            ))
        return ret

    @staticmethod
    def query_by_feed(feed_unionid, offset=None, size=10, detail=False):
        user_id, feed_id = feed_unionid
        q = UserFeed.objects.select_related('feed')\
            .filter(user_id=user_id, feed_id=feed_id)\
            .only('id', 'story_offset', 'feed_id', 'feed__id', 'feed__total_storys')
        try:
            user_feed = q.get()
        except UserFeed.DoesNotExist as ex:
            raise FeedNotFoundError() from ex
        total = user_feed.feed.total_storys
        if offset is None:
            offset = user_feed.story_offset
        q = Story.objects.filter(feed_id=feed_id, offset__gte=offset)
        if not detail:
            q = q.defer(*STORY_DETAIL_FEILDS)
        q = q.order_by('offset')[:size]
        storys = list(q.all())
        story_ids = [x.id for x in storys]
        q = UserStory.objects.filter(user_id=user_id, feed_id=feed_id, story_id__in=story_ids)
        q = q.exclude(is_favorited=False, is_watched=False)
        user_storys = list(q.all())
        ret = UnionStory._merge_storys(
            storys, user_storys, user_feeds=[user_feed], user_id=user_id, detail=detail)
        return total, offset, ret

    @staticmethod
    def query_recent_by_user(user_id, feed_unionids=None, days=14, limit=300, detail=False):
        if feed_unionids:
            feed_ids = [x.feed_id for x in feed_unionids]
            q = UserFeed.objects.only('id', 'feed_id')\
                .filter(user_id=user_id, feed_id__in=feed_ids)
            user_feeds = list(q.all())
        else:
            q = UserFeed.objects.only('id', 'feed_id')\
                .filter(user_id=user_id)
            user_feeds = list(q.all())
        feed_ids = [x.feed_id for x in q.all()]
        dt_begin = timezone.now() - timezone.timedelta(days=days)
        q = Story.objects.filter(feed_id__in=feed_ids)\
            .filter(dt_published__gte=dt_begin)
        if not detail:
            q = q.defer(*STORY_DETAIL_FEILDS)
        q = q.order_by('-dt_published')[:limit]
        storys = list(q.all())
        story_ids = [x.id for x in storys]
        q = UserStory.objects.filter(user_id=user_id, feed_id__in=feed_ids, story_id__in=story_ids)
        q = q.exclude(is_favorited=False, is_watched=False)
        user_storys = list(q.all())
        union_storys = UnionStory._merge_storys(
            storys, user_storys, user_feeds=user_feeds, user_id=user_id, detail=detail)
        return union_storys

    @staticmethod
    def _query_by_tag(user_id, is_favorited=None, is_watched=None, detail=False):
        q = UserStory.objects.select_related('story').filter(user_id=user_id)
        if not detail:
            q = q.defer(*STORY_DETAIL_FEILDS)
        if is_favorited is not None:
            q = q.filter(is_favorited=is_favorited)
        if is_watched is not None:
            q = q.filter(is_watched=is_watched)
        user_storys = list(q.all())
        storys = [x.story for x in user_storys]
        union_storys = UnionStory._merge_storys(storys, user_storys, user_id=user_id, detail=detail)
        return union_storys

    @staticmethod
    def query_favorited(user_id, detail=False):
        return UnionStory._query_by_tag(user_id, is_favorited=True, detail=detail)

    @staticmethod
    def query_watched(user_id, detail=False):
        return UnionStory._query_by_tag(user_id, is_watched=True, detail=detail)

    @staticmethod
    def _set_tag_by_id(story_unionid, is_favorited=None, is_watched=None):
        union_story = UnionStory.get_by_id(story_unionid)
        user_feed_id = union_story._user_feed_id
        user_story = union_story.user_story
        if user_story is None:
            user_id, feed_id, offset = story_unionid
            user_story = UserStory(
                user_id=user_id,
                feed_id=feed_id,
                user_feed_id=user_feed_id,
                story_id=union_story.story.id,
                offset=union_story.story.offset
            )
        if is_favorited is not None:
            user_story.is_favorited = user_story
            user_story.dt_favorited = timezone.now()
        if is_watched is not None:
            user_story.is_watched = user_story
            user_story.dt_watched = timezone.now()
        user_story.save()
        union_story._user_story = user_story
        return union_story

    @staticmethod
    def set_favorited_by_id(story_unionid, is_favorited):
        return UnionStory._set_tag_by_id(story_unionid, is_favorited=is_favorited)

    @staticmethod
    def set_watched_by_id(story_unionid, is_watched):
        return UnionStory._set_tag_by_id(story_unionid, is_watched=is_watched)

    @staticmethod
    def set_favorited_by_feed_offset(feed_unionid, offset, is_favorited):
        story_unionid = StoryUnionId(*feed_unionid, offset)
        return UnionStory.set_favorited_by_id(story_unionid, is_favorited=is_favorited)

    @staticmethod
    def set_watched_by_feed_offset(feed_unionid, offset, is_watched):
        story_unionid = StoryUnionId(*feed_unionid, offset)
        return UnionStory.set_watched_by_id(story_unionid, is_watched=is_watched)
