package kenxuflow

import (
	"context"
	"encoding/json"
	statsimpl "github.com/xtls/xray-core/app/stats"
	xnet "github.com/xtls/xray-core/common/net"
	statsapi "github.com/xtls/xray-core/features/stats"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func fixtureFlow(id string) *Flow {
	return &Flow{id: id, email: "kenxu:fixture:a", source: "192.0.2.1", target: xnet.TCPDestination(xnet.DomainAddress("example.test"), 443)}
}
func TestFrozenSnapshotAckRetainsNewBytesAndClose(t *testing.T) {
	m := NewManager(100)
	f := fixtureFlow("a")
	now := time.Now().UnixMilli()
	m.add(f, true, 10, f.target, false, now)
	first := m.Snapshot(now)
	m.add(f, true, 20, f.target, true, now)
	again := m.Snapshot(now)
	a, _ := json.Marshal(first)
	b, _ := json.Marshal(again)
	if string(a) != string(b) {
		t.Fatal("Unacknowledged snapshot changed")
	}
	if m.Ack(first.Epoch, first.Seq) != nil || m.Ack(first.Epoch, first.Seq) != nil {
		t.Fatal("Ack must be idempotent")
	}
	second := m.Snapshot(now)
	if len(second.Records) != 1 || second.Records[0].Up != 20 || !second.Records[0].Closed {
		t.Fatalf("Lost bytes or final state: %+v", second)
	}
	second.Records[0].Up = 999
	if m.Snapshot(now).Records[0].Up != 20 {
		t.Fatal("Caller mutated internal snapshot")
	}
}
func TestCloseAfterSnapshotIsNotDropped(t *testing.T) {
	m := NewManager(100)
	f := fixtureFlow("close")
	now := time.Now().UnixMilli()
	m.add(f, false, 10, f.target, false, now)
	snap := m.Snapshot(now)
	m.add(f, false, 0, f.target, true, now)
	m.Ack(snap.Epoch, snap.Seq)
	next := m.Snapshot(now)
	if len(next.Records) != 1 || next.Records[0].Down != 0 || !next.Records[0].Closed {
		t.Fatal("Missing final close")
	}
}
func TestTenThousandClosedFlowsAreDrainedWithoutLoss(t *testing.T) {
	m := NewManager(65536)
	now := time.Now().UnixMilli()
	for i := 0; i < 10000; i++ {
		f := fixtureFlow(string(rune(i + 1)))
		m.add(f, false, 100, f.target, true, now)
	}
	var total int64
	for {
		snap := m.Snapshot(now)
		if len(snap.Records) == 0 {
			break
		}
		if len(snap.Records) > 1000 {
			t.Fatal("Unbounded batch")
		}
		for _, r := range snap.Records {
			total += r.Down
		}
		if m.Ack(snap.Epoch, snap.Seq) != nil {
			t.Fatal("Ack failed")
		}
	}
	if total != 1000000 {
		t.Fatalf("Wrong bytes: %d", total)
	}
}
func TestCapacityDegradesToExplicitUnknownNotLostBytes(t *testing.T) {
	m := NewManager(1)
	now := time.Now().UnixMilli()
	m.add(fixtureFlow("a"), true, 10, fixtureFlow("a").target, true, now)
	m.add(fixtureFlow("b"), true, 20, fixtureFlow("b").target, true, now)
	snap := m.Snapshot(now)
	var total int64
	unknown := false
	for _, r := range snap.Records {
		total += r.Up
		if r.Quality == "unknown-capacity" {
			unknown = true
			if r.Host != "" || r.Source != "" {
				t.Fatal("Unknown must not invent labels")
			}
		}
	}
	if total != 30 || !unknown {
		t.Fatal("Dropped bytes on capacity")
	}
}
func TestRetentionRedactsFrozenAndQueuedMetadataButNotBytes(t *testing.T) {
	m := NewManager(100)
	now := time.Now().UnixMilli()
	f := fixtureFlow("old")
	m.add(f, true, 33, f.target, true, now-31*86400000)
	snap := m.Snapshot(now - 31*86400000)
	m.redact(now)
	expired := m.Snapshot(now)
	if expired.Records[0].Host != "" || expired.Records[0].Source != "" || expired.Records[0].Up != 33 {
		t.Fatal("Retention failed")
	}
	if m.Ack(snap.Epoch, snap.Seq) != nil || len(m.Snapshot(now).Records) != 0 {
		t.Fatal("Redaction broke acknowledgement")
	}
}
func TestObserverHttpRejectsInvalidAckAndMethods(t *testing.T) {
	m := NewManager(1)
	for _, tc := range []struct {
		method, path, body string
		status             int
	}{{"POST", "/snapshot", "", 405}, {"GET", "/ack", "", 405}, {"POST", "/ack", "{}", 409}} {
		r := httptest.NewRequest(tc.method, tc.path, strings.NewReader(tc.body))
		w := httptest.NewRecorder()
		m.Handler().ServeHTTP(w, r)
		if w.Code != tc.status {
			t.Fatalf("%+v: %d", tc, w.Code)
		}
	}
}
func TestGracefulCheckpointRetainsUnacknowledgedBytes(t *testing.T) {
	m := NewManager(1000)
	dir := t.TempDir()
	now := time.Now().UnixMilli()
	for i := 0; i < 600; i++ {
		f := fixtureFlow(string(rune(i + 1)))
		m.add(f, true, 17, f.target, true, now)
	}
	if err := m.Checkpoint(dir); err != nil {
		t.Fatal(err)
	}
	files, _ := filepath.Glob(filepath.Join(dir, "*.json"))
	var total int64
	for _, file := range files {
		raw, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		var s Snapshot
		if json.Unmarshal(raw, &s) != nil {
			t.Fatal("Invalid checkpoint")
		}
		for _, row := range s.Records {
			total += row.Up
		}
		info, _ := os.Stat(file)
		if info.Mode().Perm() != 0600 {
			t.Fatal("Checkpoint not private")
		}
	}
	if total != 600*17 || len(m.Snapshot(now).Records) != 0 {
		t.Fatal("Graceful checkpoint lost or retained acknowledged bytes")
	}
}
func TestConcurrentWritersSnapshotAndAckConserveBytes(t *testing.T) {
	m := NewManager(1000)
	var wg sync.WaitGroup
	var done atomic.Bool
	var observed int64
	readerDone := make(chan bool)
	go func() {
		for {
			snap := m.Snapshot(time.Now().UnixMilli())
			if len(snap.Records) == 0 {
				if done.Load() {
					break
				}
				time.Sleep(time.Millisecond)
				continue
			}
			for _, r := range snap.Records {
				observed += r.Up + r.Down
			}
			if m.Ack(snap.Epoch, snap.Seq) != nil {
				panic("Concurrent Ack failed")
			}
		}
		readerDone <- true
	}()
	for i := 0; i < 32; i++ {
		wg.Add(1)
		go func(index int) {
			defer wg.Done()
			f := fixtureFlow(string(rune(index + 1)))
			for j := 0; j < 1000; j++ {
				m.add(f, j%2 == 0, 1, f.target, j == 999, time.Now().UnixMilli())
			}
		}(i)
	}
	wg.Wait()
	done.Store(true)
	<-readerDone
	if observed != 32000 {
		t.Fatalf("Concurrent bytes: %d", observed)
	}
}
func TestMinuteAndMonthBucketsDoNotMoveBytesToCloseTime(t *testing.T) {
	m := NewManager(100)
	f := fixtureFlow("month")
	before := time.Date(2026, 10, 31, 15, 59, 59, 0, time.UTC).UnixMilli()
	m.add(f, true, 10, f.target, false, before)
	m.add(f, false, 20, f.target, true, before+2000)
	s := m.Snapshot(before + 2000)
	if len(s.Records) != 2 || s.Records[0].At == s.Records[1].At {
		t.Fatal("Lost cross-month boundary")
	}
	var up, down int64
	for _, r := range s.Records {
		if r.Up > 0 {
			up = r.At
		}
		if r.Down > 0 {
			down = r.At
		}
	}
	if up >= down {
		t.Fatal("Bytes moved to closing minute")
	}
}

type coldRace struct {
	statsapi.Manager
	reads atomic.Int32
	gate  chan struct{}
}

func (m *coldRace) GetCounter(name string) statsapi.Counter {
	value := m.Manager.GetCounter(name)
	n := m.reads.Add(1)
	if n <= 2 {
		if n == 2 {
			close(m.gate)
		}
		<-m.gate
	}
	return value
}
func TestConcurrentInitialCounterRegistrationCannotDropAConnection(t *testing.T) {
	base, err := statsimpl.NewManager(context.Background(), &statsimpl.Config{})
	if err != nil {
		t.Fatal(err)
	}
	m := &coldRace{Manager: base, gate: make(chan struct{})}
	results := make(chan statsapi.Counter, 2)
	for i := 0; i < 2; i++ {
		go func() { value, _ := statsapi.GetOrRegisterCounter(m, "fixture"); results <- value }()
	}
	for i := 0; i < 2; i++ {
		c := <-results
		if c == nil {
			t.Fatal("Concurrent first connection lost its counter")
		}
		c.Add(10)
	}
	if m.Manager.GetCounter("fixture").Value() != 20 {
		t.Fatal("Concurrent first bytes lost")
	}
}
