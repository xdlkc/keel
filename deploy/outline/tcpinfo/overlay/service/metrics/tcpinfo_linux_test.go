// Copyright 2026 Keel contributors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

//go:build linux

package metrics

import (
	"bytes"
	"io"
	"net"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/stretchr/testify/require"
)

func TestTCPInfoLoopbackAndFinalSample(t *testing.T) {
	listener, err := net.ListenTCP("tcp4", &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1)})
	require.NoError(t, err)
	defer listener.Close()

	type acceptResult struct {
		conn *net.TCPConn
		err  error
	}
	accepted := make(chan acceptResult, 1)
	go func() {
		conn, err := listener.AcceptTCP()
		accepted <- acceptResult{conn: conn, err: err}
	}()
	client, err := net.DialTCP("tcp4", nil, listener.Addr().(*net.TCPAddr))
	require.NoError(t, err)
	defer client.Close()
	accept := <-accepted
	require.NoError(t, accept.err)
	server := accept.conn
	defer server.Close()

	collector := NewTCPInfoCollector()
	registry := prometheus.NewPedanticRegistry()
	registry.MustRegister(collector)
	device := collector.AddDevice("loopback", client)
	source := collector.AddSource("loopback", server)
	require.NotNil(t, device)
	require.NotNil(t, source)

	upload := bytes.Repeat([]byte("u"), 32*1024)
	download := bytes.Repeat([]byte("d"), 16*1024)
	relayResult := make(chan error, 1)
	go func() {
		if _, err := io.CopyN(io.Discard, server, int64(len(upload))); err != nil {
			relayResult <- err
			return
		}
		_, err := server.Write(download)
		relayResult <- err
	}()
	_, err = client.Write(upload)
	require.NoError(t, err)
	_, err = io.ReadFull(client, make([]byte, len(download)))
	require.NoError(t, err)
	require.NoError(t, <-relayResult)

	sampler := newTCPInfoSampler(client)
	require.NotNil(t, sampler)
	var sample tcpInfoSample
	require.Eventually(t, func() bool {
		var err error
		sample, err = sampler.Sample()
		return err == nil && sample.rtt > 0 &&
			sample.bytesAcked >= uint64(len(upload)) &&
			sample.bytesReceived >= uint64(len(download))
	}, 2*time.Second, 10*time.Millisecond)

	device.Close()
	source.Close()
	values := gatherTCPMetrics(t, registry)
	require.GreaterOrEqual(t,
		values[tcpMetricID{name: "keel_tcp_download_bytes_total", key: "loopback", leg: "device"}],
		float64(len(upload)),
	)
	require.GreaterOrEqual(t,
		values[tcpMetricID{name: "keel_tcp_download_bytes_total", key: "loopback", leg: "source"}],
		float64(len(upload)),
	)
	for _, leg := range []string{"device", "source"} {
		require.Zero(t, values[tcpMetricID{name: "keel_tcp_rtt_seconds_sum", key: "loopback", leg: leg}])
		require.Zero(t, values[tcpMetricID{name: "keel_tcp_rtt_samples", key: "loopback", leg: leg}])
	}
	require.Zero(t, values[tcpMetricID{name: "keel_tcp_device_notsent_bytes", key: "loopback"}])
}
