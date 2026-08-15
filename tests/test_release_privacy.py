import gzip
import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import check_release_privacy as privacy  # noqa: E402


class ReleasePrivacyTest(unittest.TestCase):
    def _repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "README.md").write_text("public fixture\n")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"],
                       check=True)
        return repo

    def test_clean_tree_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            self.assertEqual(privacy.audit_tree(repo, []), [])

    def test_tracked_local_task_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            task = repo / "benchmark/tasks/local-secret.json"
            task.parent.mkdir(parents=True)
            task.write_text("{}")
            subprocess.run(["git", "-C", str(repo), "add", "-f", str(task)],
                           check=True)
            failures = privacy.audit_tree(repo, [])
            self.assertTrue(any("private-only path" in item
                                for item in failures))
            self.assertNotIn("local-secret", "\n".join(failures))

    def test_private_tree_path_is_redacted_when_payload_also_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            protected = b"combined-tree-protected-fixture"
            private_name = "benchmark/tasks/local-combined-secret.json"
            task = repo / private_name
            task.parent.mkdir(parents=True)
            task.write_bytes(protected)
            subprocess.run(["git", "-C", str(repo), "add", "-f",
                            private_name], check=True)

            joined = "\n".join(privacy.audit_tree(repo, [protected]))

            self.assertIn("matches protected denylist entry", joined)
            self.assertNotIn(private_name, joined)
            self.assertNotIn(protected.decode(), joined)

    def test_untracked_publishable_file_is_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            protected = b"untracked-protected-fixture"
            (repo / "new.txt").write_bytes(protected)
            failures = privacy.audit_tree(repo, [protected])
            self.assertTrue(any(item.startswith("new.txt (worktree):")
                                for item in failures))

    def test_index_and_worktree_payloads_are_both_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            path = repo / "README.md"
            staged = b"staged-protected-fixture"
            unstaged = b"unstaged-protected-fixture"
            path.write_bytes(staged)
            subprocess.run(["git", "-C", str(repo), "add", "README.md"],
                           check=True)
            path.write_bytes(unstaged)

            staged_failures = privacy.audit_tree(repo, [staged])
            unstaged_failures = privacy.audit_tree(repo, [unstaged])

            self.assertTrue(any("(index)" in item
                                for item in staged_failures))
            self.assertTrue(any("(worktree)" in item
                                for item in unstaged_failures))

    def test_index_and_worktree_symlink_targets_are_scanned_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            staged_marker = "synthetic-staged-link-target"
            worktree_marker = "synthetic-worktree-link-target"
            link = repo / "public-link"
            link.symlink_to(f"benchmark/results/{staged_marker}")
            subprocess.run(["git", "-C", str(repo), "add", "public-link"],
                           check=True)
            link.unlink()
            link.symlink_to(f"benchmark/archive/{worktree_marker}")

            joined = "\n".join(privacy.audit_tree(
                repo, [staged_marker.encode(), worktree_marker.encode()]))

            self.assertIn("indexed symlink target: name matches", joined)
            self.assertIn("worktree symlink target: name matches", joined)
            self.assertGreaterEqual(joined.count("private-only path"), 2)
            self.assertNotIn(staged_marker, joined)
            self.assertNotIn(worktree_marker, joined)

    def test_publishable_filename_is_scanned_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            protected = "protected-path-fixture"
            path = repo / f"prefix-{protected}-suffix.txt"
            path.write_text("benign payload\n")

            failures = privacy.audit_tree(repo, [protected.encode()])
            joined = "\n".join(failures)

            self.assertIn("publishable path: name matches", joined)
            self.assertNotIn(protected, joined)

    def test_denylist_match_does_not_echo_protected_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            protected = "private-marker-for-test"
            (repo / "README.md").write_text(protected)
            denylist = root / "deny.txt"
            denylist.write_text(protected + "\n")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = privacy.main(["--root", str(repo),
                                     "--denylist", str(denylist)])
            self.assertEqual(code, 1)
            self.assertNotIn(protected, stderr.getvalue())
            self.assertIn("entry 1", stderr.getvalue())

    def test_zip_and_tar_payloads_are_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            term = b"protected-fixture-value"
            wheel = root / "sample.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("pkg/METADATA", term)
                archive.writestr(
                    "source/benchmark/tasks/local-secret.json", b"{}")
            source = root / "sample.tar.gz"
            with tarfile.open(source, "w:gz") as archive:
                info = tarfile.TarInfo("pkg/README.md")
                info.size = len(term)
                archive.addfile(info, io.BytesIO(term))
            self.assertTrue(privacy.audit_artifact(wheel, [term]))
            self.assertTrue(privacy.audit_artifact(source, [term]))
            self.assertTrue(any("private-only path" in failure
                                for failure in privacy.audit_artifact(wheel, [])))
            self.assertNotIn("local-secret", "\n".join(
                privacy.audit_artifact(wheel, [])))

    def test_private_archive_member_is_redacted_when_payload_also_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protected = b"combined-archive-protected-fixture"
            private_name = "source/benchmark/tasks/local-combined-secret.json"
            wheel = root / "sample.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(private_name, protected)

            joined = "\n".join(privacy.audit_artifact(wheel, [protected]))

            self.assertIn("matches protected denylist entry", joined)
            self.assertNotIn(private_name, joined)
            self.assertNotIn(protected.decode(), joined)

    def test_archive_member_names_and_link_targets_are_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protected = "protected-member-fixture"
            source = root / "sample.tar.gz"
            with tarfile.open(source, "w:gz") as archive:
                regular = tarfile.TarInfo(f"pkg/{protected}.txt")
                regular.size = 0
                archive.addfile(regular, io.BytesIO())
                link = tarfile.TarInfo("pkg/public-link")
                link.type = tarfile.SYMTYPE
                link.linkname = protected
                archive.addfile(link)

            failures = privacy.audit_artifact(source, [protected.encode()])
            joined = "\n".join(failures)

            self.assertIn("archive member: name matches", joined)
            self.assertIn("matches protected denylist entry", joined)
            self.assertNotIn(protected, joined)

    def test_zip_symlink_target_is_scanned_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = "synthetic-zip-symlink-target"
            wheel = root / "sample.whl"
            link = zipfile.ZipInfo("pkg/public-link")
            link.create_system = 3
            link.external_attr = (0o120777 << 16)
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    link, f"benchmark/results/{marker}".encode())

            joined = "\n".join(
                privacy.audit_artifact(wheel, [marker.encode()]))

            self.assertIn("archive link target: name matches", joined)
            self.assertIn("private-only path", joined)
            self.assertNotIn(marker, joined)

    def test_tar_symlink_and_hardlink_targets_are_scanned_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            symlink_marker = "synthetic-tar-symlink-target"
            hardlink_marker = "synthetic-tar-hardlink-target"
            source = root / "sample.tar.gz"
            with tarfile.open(source, "w:gz") as archive:
                symlink = tarfile.TarInfo("pkg/public-symlink")
                symlink.type = tarfile.SYMTYPE
                symlink.linkname = f"benchmark/results/{symlink_marker}"
                archive.addfile(symlink)
                hardlink = tarfile.TarInfo("pkg/public-hardlink")
                hardlink.type = tarfile.LNKTYPE
                hardlink.linkname = f"benchmark/archive/{hardlink_marker}"
                archive.addfile(hardlink)

            joined = "\n".join(privacy.audit_artifact(
                source, [symlink_marker.encode(), hardlink_marker.encode()]))

            self.assertIn("archive link target: name matches", joined)
            self.assertGreaterEqual(joined.count("private-only path"), 2)
            self.assertNotIn(symlink_marker, joined)
            self.assertNotIn(hardlink_marker, joined)

    def test_empty_zip_directory_name_is_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = "synthetic-zip-directory-marker"
            wheel = root / "sample.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.mkdir(f"pkg/{marker}/")

            joined = "\n".join(
                privacy.audit_artifact(wheel, [marker.encode()]))

            self.assertIn("archive member: name matches", joined)
            self.assertNotIn(marker, joined)

    def test_empty_tar_directory_private_name_is_scanned_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private_name = "source/benchmark/results/"
            source = root / "sample.tar.gz"
            with tarfile.open(source, "w:gz") as archive:
                directory = tarfile.TarInfo(private_name)
                directory.type = tarfile.DIRTYPE
                archive.addfile(directory)

            joined = "\n".join(privacy.audit_artifact(source, []))

            self.assertIn("contains private-only path", joined)
            self.assertNotIn(private_name, joined)

    def test_zip_comments_and_extra_fields_are_scanned_with_redacted_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_marker = b"synthetic-zip-archive-comment"
            member_marker = b"synthetic-zip-member-comment"
            extra_marker = b"synthetic-zip-extra-field"
            wheel = root / "sample.whl"
            info = zipfile.ZipInfo("pkg/public.txt")
            info.comment = member_marker
            info.extra = (b"\xfe\xca" + len(extra_marker).to_bytes(2, "little")
                          + extra_marker)
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.comment = archive_marker
                archive.writestr(info, b"public fixture\n")

            joined = "\n".join(privacy.audit_artifact(
                wheel, [archive_marker, member_marker, extra_marker]))

            self.assertGreaterEqual(
                joined.count("archive metadata location redacted"), 3)
            for marker in (archive_marker, member_marker, extra_marker):
                self.assertNotIn(marker.decode(), joined)

    def test_tar_owner_and_pax_metadata_are_scanned_with_redacted_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_marker = "synthetic-tar-user-marker"
            group_marker = "synthetic-tar-group-marker"
            pax_marker = "synthetic-tar-pax-marker"
            source = root / "sample.tar.gz"
            with tarfile.open(
                    source, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                info = tarfile.TarInfo("pkg/public.txt")
                info.size = 0
                info.uname = user_marker
                info.gname = group_marker
                info.pax_headers = {"synthetic.key": pax_marker}
                archive.addfile(info, io.BytesIO())

            joined = "\n".join(privacy.audit_artifact(
                source, [user_marker.encode(), group_marker.encode(),
                         pax_marker.encode()]))

            self.assertGreaterEqual(
                joined.count("archive metadata location redacted"), 3)
            for marker in (user_marker, group_marker, pax_marker):
                self.assertNotIn(marker, joined)

    def test_gzip_wrapper_filename_is_scanned_with_redacted_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = "synthetic-gzip-filename-marker"
            inner = io.BytesIO()
            with tarfile.open(fileobj=inner, mode="w") as archive:
                info = tarfile.TarInfo("pkg/public.txt")
                info.size = 0
                archive.addfile(info, io.BytesIO())
            wrapped = io.BytesIO()
            with gzip.GzipFile(filename=marker, mode="wb", fileobj=wrapped,
                               mtime=0) as stream:
                stream.write(inner.getvalue())
            source = root / "sample.tar.gz"
            source.write_bytes(wrapped.getvalue())

            joined = "\n".join(
                privacy.audit_artifact(source, [marker.encode()]))

            self.assertIn("artifact container location redacted", joined)
            self.assertNotIn(marker, joined)

    def test_nested_archive_payload_is_rejected_with_redacted_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = "synthetic-nested-archive-marker"
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w") as archive:
                archive.writestr("public.txt", b"public fixture\n")
            wheel = root / "sample.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(f"pkg/{marker}.bin", inner.getvalue())

            joined = "\n".join(
                privacy.audit_artifact(wheel, [marker.encode()]))

            self.assertIn("nested archive payload is not allowed", joined)
            self.assertNotIn(marker, joined)

    def test_compressed_archive_blob_is_rejected_in_tree_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            marker = b"synthetic-compressed-blob-marker"
            bundle = io.BytesIO()
            with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
                info = tarfile.TarInfo("private.txt")
                info.size = len(marker)
                archive.addfile(info, io.BytesIO(marker))
            path = repo / "bundle.bin"
            path.write_bytes(bundle.getvalue())

            tree_joined = "\n".join(privacy.audit_tree(repo, [marker]))
            self.assertIn("archive payload is not allowed", tree_joined)
            self.assertNotIn(marker.decode(), tree_joined)

            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=test",
                 "-c", "user.email=test@example.invalid", "add", path.name],
                check=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=test",
                 "-c", "user.email=test@example.invalid", "commit", "-qm",
                 "add compressed fixture"], check=True)
            path.unlink()
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=test",
                 "-c", "user.email=test@example.invalid", "commit", "-qam",
                 "remove compressed fixture"], check=True)

            history_joined = "\n".join(
                privacy.audit_history(repo, [marker]))
            self.assertIn("archive payload is not allowed", history_joined)
            self.assertNotIn(marker.decode(), history_joined)

    def test_history_scan_finds_removed_content_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            protected = b"historical-protected-fixture"
            path = repo / "README.md"
            path.write_bytes(protected)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=test",
                 "-c", "user.email=test@example.invalid", "commit", "-qam",
                 "private fixture"], check=True)
            path.write_text("replacement\n")
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=test",
                 "-c", "user.email=test@example.invalid", "commit", "-qam",
                 "replacement"], check=True)
            failures = privacy.audit_history(repo, [protected])
            joined = "\n".join(failures)
            self.assertTrue(failures)
            self.assertNotIn(protected.decode(), joined)
            self.assertIn("git object", joined)

    def test_history_scan_includes_removed_filenames_via_tree_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            protected = "historical-path-fixture"
            path = repo / f"prefix-{protected}.txt"
            path.write_text("benign payload\n")
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=test",
                 "-c", "user.email=test@example.invalid", "add", path.name],
                check=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=test",
                 "-c", "user.email=test@example.invalid", "commit", "-qm",
                 "add fixture"], check=True)
            path.unlink()
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=test",
                 "-c", "user.email=test@example.invalid", "commit", "-qam",
                 "remove fixture"], check=True)

            failures = privacy.audit_history(repo, [protected.encode()])
            joined = "\n".join(failures)

            self.assertIn("(tree)", joined)
            self.assertNotIn(protected, joined)

    def test_release_workflow_runs_no_privacy_audit(self):
        """Records that the release path is deliberately unaudited.

        The scanner below still works and is worth running by hand before a
        tag; nothing in CI or the release workflow invokes it, so tracked
        files, git history, and built artifacts reach PyPI unscanned.
        """
        workflow = (Path(__file__).resolve().parents[1]
                    / ".github/workflows/release.yml").read_text()
        self.assertNotIn("check_release_privacy", workflow)


if __name__ == "__main__":
    unittest.main()
