from pathlib import Path

from app import media


def test_chat_media_dir_namespaced_per_chat(tmp_path: Path) -> None:
    root = tmp_path
    dir_a = media.chat_media_dir(123, root=root)
    dir_b = media.chat_media_dir(456, root=root)
    assert dir_a == root / "123"
    assert dir_b == root / "456"
    assert dir_a != dir_b


def test_photo_dest_inside_chat_dir(tmp_path: Path) -> None:
    root = tmp_path
    chat_dir = media.chat_media_dir(123, root=root)
    photo = media.photo_dest(123, root=root)
    assert photo == chat_dir / "photo.jpg"


def test_avatar_dest_inside_chat_dir(tmp_path: Path) -> None:
    root = tmp_path
    chat_dir = media.chat_media_dir(123, root=root)
    avatar = media.avatar_dest(123, root=root)
    assert avatar == chat_dir / "avatar.png"


def test_purge_chat_media_removes_photo_and_avatar(tmp_path: Path) -> None:
    root = tmp_path
    chat_dir = media.chat_media_dir(123, root=root)
    chat_dir.mkdir(parents=True, exist_ok=True)
    (chat_dir / "photo.jpg").write_text("portrait")
    (chat_dir / "avatar.png").write_text("generated")

    media.purge_chat_media(123, root=root)

    assert not chat_dir.exists()


def test_purge_chat_media_removes_only_that_chat(tmp_path: Path) -> None:
    root = tmp_path
    chat_dir_a = media.chat_media_dir(123, root=root)
    chat_dir_b = media.chat_media_dir(456, root=root)
    chat_dir_a.mkdir(parents=True, exist_ok=True)
    chat_dir_b.mkdir(parents=True, exist_ok=True)
    (chat_dir_a / "photo.jpg").write_text("a")
    (chat_dir_b / "photo.jpg").write_text("b")
    media.purge_chat_media(123, root=root)
    assert not (chat_dir_a / "photo.jpg").exists()
    assert (chat_dir_b / "photo.jpg").exists()


def test_purge_chat_media_idempotent_and_tolerates_missing(tmp_path: Path) -> None:
    root = tmp_path
    # missing dir is fine
    media.purge_chat_media(999, root=root)
    # create and purge twice
    chat_dir = media.chat_media_dir(123, root=root)
    chat_dir.mkdir(parents=True, exist_ok=True)
    (chat_dir / "photo.jpg").write_text("a")
    media.purge_chat_media(123, root=root)
    media.purge_chat_media(123, root=root)
    assert not chat_dir.exists()
