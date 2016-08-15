"""
Models used for robust grading.

Robust grading allows student scores to be saved per-subsection independent
of any changes that may occur to the course after the score is achieved.
"""

from base64 import b64encode
from collections import namedtuple
from hashlib import sha256
import json
import logging
from operator import attrgetter

from django.db import models
from django.db.utils import IntegrityError
from model_utils.models import TimeStampedModel

from coursewarehistoryextended.fields import UnsignedBigIntAutoField
from xmodule_django.models import CourseKeyField, UsageKeyField


log = logging.getLogger(__name__)


# Used to serialize information about a block at the time it was used in
# grade calculation.
BlockRecord = namedtuple('BlockRecord', ['locator', 'weight', 'max_score'])


class BlockRecordSet(frozenset):
    """
    An immutable ordered collection of BlockRecord objects.
    """

    def __init__(self, *args, **kwargs):
        super(BlockRecordSet, self).__init__(*args, **kwargs)
        self._json = None
        self._hash = None

    def to_json(self):
        """
        Return a JSON-serialized version of the list of block records, using a
        stable ordering.
        """
        if self._json is None:
            sorted_blocks = sorted(self, key=attrgetter('locator'))
            # Remove spaces from separators for more compact representation
            self._json = json.dumps(
                [block._asdict() for block in sorted_blocks],
                separators=(',', ':'),
                sort_keys=True,
            )
        return self._json

    @classmethod
    def from_json(cls, blockrecord_json):
        """
        Return a BlockRecordSet from a json list.
        """
        block_dicts = json.loads(blockrecord_json)
        record_generator = (BlockRecord(**block) for block in block_dicts)
        return cls(record_generator)

    def to_hash(self):
        """
        Return a hashed version of the list of block records.

        This currently hashes using sha256, and returns a base64 encoded version
        of the binary digest.  In the future, different algorithms could be
        supported by adding a label indicated which algorithm was used, e.g.,
        "sha1$witfkXg0JglCjW9RssWvTAveakI=".
        """
        if self._hash is None:
            self._hash = b64encode(sha256(self.to_json()).digest())
        return self._hash


class VisibleBlocksQuerySet(models.QuerySet):
    """
    A custom QuerySet representing VisibleBlocks.
    """
    def create_from_blockrecords(self, blocks):
        """
        Creates a new VisibleBlocks model object.

        Argument 'blocks' should be an iterable collection of BlockRecords.
        """
        if not isinstance(blocks, BlockRecordSet):
            blocks = BlockRecordSet(blocks)
        model, _ = self.get_or_create(hashed=blocks.to_hash(), defaults={'blocks_json': blocks.to_json()})
        return model


class VisibleBlocks(models.Model):
    """
    A django model used to track the state of a set of visible blocks under a
    given subsection at the time they are used for grade calculation.

    This state is represented using an array of serialized BlockRecords, stored
    in the blocks_json field. A hash of this json array is used for lookup
    purposes.
    """
    blocks_json = models.TextField()
    hashed = models.CharField(max_length=44, unique=True)

    objects = VisibleBlocksQuerySet.as_manager()

    def __unicode__(self):
        """
        String representation of this model.
        """
        return "VisibleBlocks object - hash:{}, raw json:'{}'".format(self.hashed, self.blocks_json)

    @property
    def blocks(self):
        """
        Returns the blocks_json data stored on this model as a list of
        BlockRecords in the order they were provided.
        """
        return BlockRecordSet.from_json(self.blocks_json)


class PersistentSubsectionGradeQuerySet(models.QuerySet):
    """
    A custom QuerySet, that handles creating a VisibleBlocks model on creation, and
    extracts the course id from the provided usage_key.
    """
    def create(self, **kwargs):
        """
        Instantiates a new model instance after creating a VisibleBlocks instance.

        Arguments:
            user_id (int)
            usage_key (serialized UsageKey)
            course_version (str)
            subtree_edited_date (datetime)
            earned_all (float)
            possible_all (float)
            earned_graded (float)
            possible_graded (float)
            visible_blocks (iterable of BlockRecord)
        """
        visible_blocks = kwargs.pop('visible_blocks')
        if not isinstance(visible_blocks, BlockRecordSet):
            visible_blocks = BlockRecordSet(visible_blocks)
        try:
            visible_blocks_model = VisibleBlocks.objects.create_from_blockrecords(blocks=visible_blocks)
        except IntegrityError:
            # If an integrity error occurs, the VisibleBlocks model we want to
            # create already exists.  Use the hash for our current list of
            # blocks as the foreign key.
            visible_blocks_hash = visible_blocks.to_hash()
        else:
            visible_blocks_hash = visible_blocks_model.hashed

        grade = self.model(
            course_id=kwargs['usage_key'].course_key,
            visible_blocks_id=visible_blocks_hash,
            **kwargs
        )
        grade.full_clean()
        grade.save()
        return grade


class PersistentSubsectionGrade(TimeStampedModel):
    """
    A django model tracking persistent grades at the subsection level.
    """

    class Meta(object):
        unique_together = (('user_id', 'course_id', 'usage_key'), )

    # primary key will need to be large for this table
    id = UnsignedBigIntAutoField(primary_key=True)  # pylint: disable=invalid-name

    # uniquely identify this particular grade object
    user_id = models.IntegerField(blank=False)
    course_id = CourseKeyField(blank=False, max_length=255)
    usage_key = UsageKeyField(blank=False, max_length=255)

    # Information relating to the state of content when grade was calculated
    subtree_edited_date = models.DateTimeField('last content edit timestamp', blank=False)
    course_version = models.CharField('guid of latest course version', blank=False, max_length=255)

    # earned/possible refers to the number of points achieved and available to achieve.
    # graded refers to the subset of all problems that are marked as being graded.
    earned_all = models.FloatField(blank=False)
    possible_all = models.FloatField(blank=False)
    earned_graded = models.FloatField(blank=False)
    possible_graded = models.FloatField(blank=False)

    # track which blocks were visible at the time of grade calculation
    visible_blocks = models.ForeignKey(VisibleBlocks, db_column='visible_blocks_hash', to_field='hashed')

    # use custom manager
    objects = PersistentSubsectionGradeQuerySet.as_manager()

    def __unicode__(self):
        """
        Returns a string representation of this model.
        """
        return "PersistentSubsectionGrade user:{}, subsection {}. {}/{} graded, {}/{} all".format(
            self.user_id,
            self.usage_key,
            self.earned_graded,
            self.possible_graded,
            self.earned_all,
            self.possible_all,
        )

    @classmethod
    def save_grade(cls, **kwargs):
        """
        Wrapper for create_grade or update_grade, depending on which applies.
        Takes the same arguments as both of thsoe methods.
        """
        try:
            cls.update(**kwargs)
        except cls.DoesNotExist:
            cls.objects.create(**kwargs)

    @classmethod
    def read(cls, user_id, usage_key):
        """
        Reads a grade from database

        Arguments:
            user_id: The user associated with the desired grade
            usage_key: The location of the subsection associated with the desired grade

        Raises PersistentSubsectionGrade.DoesNotExist if applicable
        """
        return cls.objects.get(
            user_id=user_id,
            usage_key=usage_key,
        )

    @classmethod
    def update(
            cls,
            user_id,
            usage_key,
            course_version,
            subtree_edited_date,
            earned_all,
            possible_all,
            earned_graded,
            possible_graded,
            visible_blocks,
    ):
        """
        Updates a previously existing grade.

        Requires all the arguments listed in docstring for create_grade
        """
        grade = cls.objects.get(
            user_id=user_id,
            usage_key=usage_key,
        )

        # Thanks to repeatable read, there's a non-zero chance of a race condition ocurring on insert.
        # If that happens, the situation is unrecoverable, as we need to read a new piece of data (a visible_blocks FK)
        # that won't be visible inside the current transaction. In those cases, log the issue and abort.
        if not isinstance(visible_blocks, BlockRecordSet):
            visible_blocks = BlockRecordSet(visible_blocks)
        try:
            visible_blocks_model = VisibleBlocks.objects.create_from_blockrecords(blocks=visible_blocks)
        except IntegrityError:
            # If an integrity error occurs, the VisibleBlocks model we want to
            # create already exists.  Use the hash for our current list of
            # blocks as the foreign key.
            visible_blocks_hash = visible_blocks.to_hash()
        else:
            visible_blocks_hash = visible_blocks_model.hashed

        try:
            visible_blocks_model = VisibleBlocks.objects.create_from_blockrecords(blocks=visible_blocks)
        except IntegrityError:
            log.error("Race condition hit in robust grading data model. Unrecoverable repeatable-read issue.")
            raise

        grade.course_version = course_version
        grade.subtree_edited_date = subtree_edited_date
        grade.earned_all = earned_all
        grade.possible_all = possible_all
        grade.earned_graded = earned_graded
        grade.possible_graded = possible_graded
        grade.visible_blocks_id = visible_blocks_hash
        grade.save()
