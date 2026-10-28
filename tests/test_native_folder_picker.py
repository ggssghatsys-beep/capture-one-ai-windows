"""Exercise pure native directory validation; never opens or writes to C1."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


VALIDATE = r"""
$tokens=$null;$parseErrors=$null
$tree=[System.Management.Automation.Language.Parser]::ParseFile($env:C1_TEST_SOURCE,[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count -gt 0){throw 'Native PowerShell source does not parse'}
foreach($name in @('Fail','Folder-ExistingPath')) {
    $function=$tree.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name},$true)
    if($function.Count -ne 1){throw 'Qualified function missing or ambiguous'}
    . ([scriptblock]::Create($function[0].Extent.Text))
}
try {
    $path=Folder-ExistingPath $env:C1_TEST_DIRECTORY
    @{ok=$true;path=$path} | ConvertTo-Json -Compress
} catch {
    @{ok=$false;code=$_.Exception.Data['BridgeCode'];message=$_.Exception.Message} | ConvertTo-Json -Compress
}
"""

CONFIRM_FAKE = r"""
$tokens=$null;$parseErrors=$null
$tree=[System.Management.Automation.Language.Parser]::ParseFile($env:C1_TEST_SOURCE,[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count -gt 0){throw 'Native PowerShell source does not parse'}
foreach($name in @('Fail','Field','Names','Same','Folder-ExistingPath','Folder-AssertNavigation','Folder-ConfirmExport')) {
    $function=$tree.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name},$true)
    if($function.Count -ne 1){throw 'Qualified function missing or ambiguous'}
    . ([scriptblock]::Create($function[0].Extent.Text))
}
# No P/Invoke declarations: this test class cannot send a Windows message.
Add-Type @'
using System;
public static class C1WindowNative {
 public static int Calls; public static long Sender; public static long WParam; public static uint Message; public static bool Timeout;
 public static IntPtr SendCommandTimeout(IntPtr hwnd,uint msg,IntPtr wp,IntPtr lp,uint flags,uint timeout,out IntPtr result) {
  Calls++;Sender=lp.ToInt64();WParam=wp.ToInt64();Message=msg;result=IntPtr.Zero;return Timeout?IntPtr.Zero:new IntPtr(1);
 }
}
'@
$script:MayHaveMutated=$false
$script:fakePicker=@{dialog_hwnd=123;export_hwnd=456;ok_hwnd=789;helper_started_at='observed'}
$script:fakeNavigation=@{folder_path=$env:C1_TEST_DIRECTORY;edit_text=[IO.Path]::GetFileName($env:C1_TEST_DIRECTORY)}
function Folder-PickerInfo {return @{picker=$script:fakePicker}}
function Folder-NavigationState {return $script:fakeNavigation}
function Check-Guard {
 if($env:C1_TEST_MODE -eq 'guardfail'){Fail 'STALE_TARGET' 'Synthetic changed target'}
 if($env:C1_TEST_MODE -eq 'ownerchange'){$script:fakePicker.ok_hwnd=999}
}
$request=@{path=$env:C1_TEST_DIRECTORY;expected_picker=$script:fakePicker.Clone();guard=@{}}
if($env:C1_TEST_MODE -eq 'wrongfolder'){$script:fakeNavigation.folder_path=[IO.Path]::GetDirectoryName($env:C1_TEST_DIRECTORY)}
if($env:C1_TEST_MODE -eq 'wrongedit'){$script:fakeNavigation.edit_text='different-folder'}
if($env:C1_TEST_MODE -eq 'timeout'){[C1WindowNative]::Timeout=$true}
try {
 $result=Folder-ConfirmExport $request 1 2 @{exe='synthetic.exe'}
 @{ok=$true;result=$result;calls=[C1WindowNative]::Calls;sender=[C1WindowNative]::Sender;wparam=[C1WindowNative]::WParam;message=[C1WindowNative]::Message} | ConvertTo-Json -Depth 10 -Compress
} catch {
 @{ok=$false;code=$_.Exception.Data['BridgeCode'];message=$_.Exception.Message;calls=[C1WindowNative]::Calls;possibly_mutated=$script:MayHaveMutated} | ConvertTo-Json -Compress
}
"""


SELECT_CURRENT_FAKE = r"""
$ErrorActionPreference='Stop'
$tokens=$null;$parseErrors=$null
$tree=[System.Management.Automation.Language.Parser]::ParseFile($env:C1_TEST_SOURCE,[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count -gt 0){throw 'Native PowerShell source does not parse'}
foreach($name in @('Fail','Field','Names','Same','Assert-ForegroundAllowed','Check-ExecutionPolicy',
                  'Folder-ExistingPath','Folder-AssertNavigation','Folder-SelectCurrentExport')) {
 $function=$tree.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name},$true)
 if($function.Count -ne 1){throw 'Qualified function missing or ambiguous'}
 . ([scriptblock]::Create($function[0].Extent.Text))
}
# This fully synthetic class has no native imports and cannot reach a window.
Add-Type @'
using System;
public static class C1WindowNative {
 public static int ClearCalls, CommandCalls;
 public static long EditHandle, DialogHandle, Sender, WParam;
 public static uint ClearMessage, CommandMessage;
 public static string Edit, ClearText, Mode;
 public static IntPtr SendMessageTimeout(IntPtr hwnd,uint msg,IntPtr wp,string text,uint flags,uint timeout,out IntPtr result) {
  ClearCalls++;EditHandle=hwnd.ToInt64();ClearMessage=msg;ClearText=text;
  if(Mode!="clear_no_effect") Edit=text;
  result=Mode=="clear_rejected"?IntPtr.Zero:new IntPtr(1);
  return Mode=="clear_timeout"?IntPtr.Zero:new IntPtr(1);
 }
 public static IntPtr SendCommandTimeout(IntPtr hwnd,uint msg,IntPtr wp,IntPtr lp,uint flags,uint timeout,out IntPtr result) {
  CommandCalls++;DialogHandle=hwnd.ToInt64();Sender=lp.ToInt64();WParam=wp.ToInt64();CommandMessage=msg;
  result=IntPtr.Zero;return Mode=="command_timeout"?IntPtr.Zero:new IntPtr(1);
 }
}
'@
$script:MayHaveMutated=$false;$script:infoCalls=0;$script:navCalls=0;$script:guardCalls=0
$script:fakePicker=@{dialog_hwnd=123;export_hwnd=456;edit_hwnd=777;ok_hwnd=789;
 helper_started_at='observed';owner_hwnd=321;helper_pid=42}
[C1WindowNative]::Mode=$env:C1_TEST_MODE
[C1WindowNative]::Edit=$env:C1_TEST_DIRECTORY
function Folder-PickerInfo {
 $script:infoCalls++
 $picker=$script:fakePicker.Clone()
 if($env:C1_TEST_MODE -eq ('owner'+$script:infoCalls)){$picker.owner_hwnd=999}
 if($env:C1_TEST_MODE -eq ('pid'+$script:infoCalls)){$picker.helper_started_at='new-process'}
 return @{picker=$picker}
}
function Folder-NavigationState {
 $script:navCalls++
 $folder=$env:C1_TEST_DIRECTORY;$edit=[C1WindowNative]::Edit
 if($env:C1_TEST_MODE -eq ('folder'+$script:navCalls)){$folder=[IO.Path]::GetDirectoryName($folder)}
 if($env:C1_TEST_MODE -eq ('edit'+$script:navCalls)){
  $edit=if($script:navCalls -le 2){[IO.Path]::GetFileName($folder)}else{'new-input'}
 }
 if($env:C1_TEST_MODE -eq ('readfail'+$script:navCalls)){Fail 'FOLDER_READBACK_FAILED' 'Synthetic read timeout'}
 return @{folder_path=$folder;edit_text=$edit;hierarchy_verified=$true}
}
function Check-Guard {
 $script:guardCalls++
 if($env:C1_TEST_MODE -eq ('guard'+$script:guardCalls)){Fail 'STALE_TARGET' 'Synthetic changed target'}
}
$request=@{path=$env:C1_TEST_DIRECTORY;expected_picker=$script:fakePicker.Clone();
 expected_edit_text=$env:C1_TEST_DIRECTORY;guard=@{};background_only=($env:C1_TEST_MODE -eq 'strict')}
if($env:C1_TEST_MODE -eq 'missing_edit'){$request.Remove('expected_edit_text')}
if($env:C1_TEST_MODE -eq 'missing_picker'){$request.Remove('expected_picker')}
if($env:C1_TEST_MODE -eq 'already_empty'){
 [C1WindowNative]::Edit='';$request.expected_edit_text=''
}
try {
 Check-ExecutionPolicy $request 'select_current_export_folder'
 $result=Folder-SelectCurrentExport $request 1 2 @{exe='synthetic.exe'}
 $output=@{ok=$true;result=$result}
} catch {
 $output=@{ok=$false;code=$_.Exception.Data['BridgeCode'];message=$_.Exception.Message}
}
$output.clear_calls=[C1WindowNative]::ClearCalls;$output.command_calls=[C1WindowNative]::CommandCalls
$output.edit_handle=[C1WindowNative]::EditHandle;$output.dialog_handle=[C1WindowNative]::DialogHandle
$output.sender=[C1WindowNative]::Sender;$output.wparam=[C1WindowNative]::WParam
$output.clear_text=[C1WindowNative]::ClearText;$output.clear_message=[C1WindowNative]::ClearMessage
$output.command_message=[C1WindowNative]::CommandMessage;$output.possibly_mutated=$script:MayHaveMutated
$output | ConvertTo-Json -Depth 10 -Compress
"""


@unittest.skipUnless(os.name == "nt", "Native validation runs on Windows PowerShell")
class NativeFolderPickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def run_script(self, path, script, mode="ok"):
        env = dict(os.environ, C1_TEST_SOURCE=str(Path(__file__).parents[1] / "c1ai/native/uia.ps1"),
                   C1_TEST_DIRECTORY=str(path), C1_TEST_MODE=mode)
        executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run([str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                                env=env, capture_output=True, text=True, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def validate(self, path):
        return self.run_script(path, VALIDATE)

    def test_existing_absolute_directory(self):
        result = self.validate(self.directory)
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"].casefold(), str(self.directory).casefold())

    def test_file_is_not_a_directory(self):
        path = self.directory / "source.jpg"
        path.write_bytes(b"keep")
        self.assertEqual(self.validate(path)["code"], "INVALID_PATH")
        self.assertEqual(path.read_bytes(), b"keep")

    def test_missing_directory_is_not_created(self):
        path = self.directory / "missing"
        self.assertEqual(self.validate(path)["code"], "INVALID_PATH")
        self.assertFalse(path.exists())

    def test_relative_drive_relative_and_traversal_are_refused(self):
        child = self.directory / "child"
        child.mkdir()
        for path in ("relative", "C:relative", str(child) + "\\..", str(self.directory) + ":stream"):
            with self.subTest(path=path):
                self.assertEqual(self.validate(path)["code"], "INVALID_PATH")

    def test_reparse_point_ancestor_is_refused(self):
        actual = self.directory / "actual"
        actual.mkdir()
        (actual / "child").mkdir()
        linked = self.directory / "linked"
        try:
            linked.symlink_to(actual, target_is_directory=True)
        except OSError:
            self.skipTest("Symbolic link creation is unavailable")
        self.assertEqual(self.validate(linked / "child")["code"], "INVALID_PATH")

    def test_confirm_sends_one_bound_button_notification_without_setting_text(self):
        result = self.run_script(self.directory, CONFIRM_FAKE)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["calls"], 1)
        self.assertEqual((result["sender"], result["wparam"], result["message"]), (789, 1, 0x111))
        self.assertTrue(result["result"]["navigation_completed"])
        self.assertFalse(result["result"]["destination_verified"])

    def test_confirm_refuses_changed_folder_edit_owner_or_guard(self):
        for mode, code in (("wrongfolder", "FOLDER_NAVIGATION_MISMATCH"), ("wrongedit", "FOLDER_READBACK_FAILED"),
                           ("ownerchange", "WRONG_DIALOG"), ("guardfail", "STALE_TARGET")):
            with self.subTest(mode=mode):
                result = self.run_script(self.directory, CONFIRM_FAKE, mode)
                self.assertEqual(result["code"], code, result)
                self.assertEqual(result["calls"], 0)
                self.assertFalse(result["possibly_mutated"])

    def test_confirm_timeout_is_one_unknown_dispatch_without_retry(self):
        result = self.run_script(self.directory, CONFIRM_FAKE, "timeout")
        self.assertEqual(result["code"], "FOLDER_SUBMIT_UNCERTAIN", result)
        self.assertEqual(result["calls"], 1)
        self.assertTrue(result["possibly_mutated"])

    def test_select_current_clears_once_then_sends_one_bound_confirmation(self):
        result = self.run_script(self.directory, SELECT_CURRENT_FAKE)
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["clear_calls"], result["command_calls"]), (1, 1))
        self.assertEqual((result["edit_handle"], result["clear_message"], result["clear_text"]), (777, 0xC, ""))
        self.assertEqual((result["dialog_handle"], result["sender"], result["wparam"], result["command_message"]),
                         (123, 789, 1, 0x111))
        self.assertTrue(result["result"]["edit_cleared_verified"])
        self.assertTrue(result["result"]["current_directory_verified"])
        self.assertTrue(result["result"]["selection_submitted"])
        self.assertFalse(result["result"]["destination_verified"])

    def test_select_current_refuses_stale_owner_target_navigation_or_missing_evidence_before_clear(self):
        modes = [("owner1", "WRONG_DIALOG"), ("owner2", "WRONG_DIALOG"),
                 ("pid1", "WRONG_DIALOG"), ("pid2", "WRONG_DIALOG"),
                 ("guard1", "STALE_TARGET"), ("folder1", "FOLDER_NAVIGATION_MISMATCH"),
                 ("folder2", "FOLDER_NAVIGATION_MISMATCH"), ("edit1", "FOLDER_EDIT_CHANGED"),
                 ("edit2", "FOLDER_EDIT_CHANGED"), ("missing_edit", "FOLDER_EVIDENCE_REQUIRED"),
                 ("missing_picker", "FOLDER_EVIDENCE_REQUIRED"), ("strict", "BACKGROUND_ONLY"),
                 ("already_empty", "FOLDER_READBACK_FAILED")]
        for mode, code in modes:
            with self.subTest(mode=mode):
                result = self.run_script(self.directory, SELECT_CURRENT_FAKE, mode)
                self.assertEqual(result["code"], code, result)
                self.assertEqual((result["clear_calls"], result["command_calls"]), (0, 0))
                self.assertFalse(result["possibly_mutated"])

    def test_select_current_clear_ack_or_readback_failure_does_not_confirm_or_retry(self):
        modes = [("clear_timeout", "FOLDER_CLEAR_UNCERTAIN"), ("clear_rejected", "FOLDER_CLEAR_UNCERTAIN"),
                 ("clear_no_effect", "FOLDER_CLEAR_UNVERIFIED"), ("readfail3", "FOLDER_READBACK_FAILED"),
                 ("readfail4", "FOLDER_READBACK_FAILED")]
        for mode, code in modes:
            with self.subTest(mode=mode):
                result = self.run_script(self.directory, SELECT_CURRENT_FAKE, mode)
                self.assertEqual(result["code"], code, result)
                self.assertEqual((result["clear_calls"], result["command_calls"]), (1, 0))
                self.assertTrue(result["possibly_mutated"])

    def test_select_current_rechecks_owner_guard_and_navigation_after_clear(self):
        modes = [("owner3", "WRONG_DIALOG"), ("owner4", "WRONG_DIALOG"),
                 ("pid3", "WRONG_DIALOG"), ("pid4", "WRONG_DIALOG"), ("guard2", "STALE_TARGET"),
                 ("folder3", "FOLDER_CLEAR_UNVERIFIED"), ("folder4", "FOLDER_CLEAR_UNVERIFIED"),
                 ("edit3", "FOLDER_CLEAR_UNVERIFIED"), ("edit4", "FOLDER_CLEAR_UNVERIFIED")]
        for mode, code in modes:
            with self.subTest(mode=mode):
                result = self.run_script(self.directory, SELECT_CURRENT_FAKE, mode)
                self.assertEqual(result["code"], code, result)
                self.assertEqual((result["clear_calls"], result["command_calls"]), (1, 0))
                self.assertTrue(result["possibly_mutated"])

    def test_select_current_command_timeout_has_one_dispatch_and_no_retry(self):
        result = self.run_script(self.directory, SELECT_CURRENT_FAKE, "command_timeout")
        self.assertEqual(result["code"], "FOLDER_SUBMIT_UNCERTAIN", result)
        self.assertEqual((result["clear_calls"], result["command_calls"]), (1, 1))
        self.assertTrue(result["possibly_mutated"])


if __name__ == "__main__":
    unittest.main()
