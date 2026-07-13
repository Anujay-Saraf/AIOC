import inspect
END='__end__'
class _GraphView:
    def __init__(self, nodes, edges, cond): self.nodes=nodes; self.edges=edges; self.cond=cond
    def draw_mermaid(self):
        lines=['graph TD']
        for n in self.nodes: lines.append(f'  {n}[{n}]')
        for a,b in self.edges: lines.append(f'  {a} --> {b}')
        for src,(_,mapping) in self.cond.items():
            for k,d in mapping.items(): lines.append(f'  {src} -- {k} --> {d}')
        return '\n'.join(lines)
class _Compiled:
    def __init__(self, nodes, entry, edges, cond, state_cls): self.nodes=nodes; self.entry=entry; self.edges=edges; self.cond=cond; self.state_cls=state_cls
    def get_graph(self): return _GraphView(self.nodes, self.edges, self.cond)
    async def ainvoke(self, initial, config=None):
        state = initial if isinstance(initial,self.state_cls) else self.state_cls(**initial)
        node=self.entry; limit=(config or {}).get('recursion_limit',60); steps=0
        while node != END and steps < limit:
            fn=self.nodes[node]
            res=fn(state)
            if inspect.isawaitable(res): res=await res
            if isinstance(res, dict):
                # preserve set fields
                state=self.state_cls(**res)
            elif isinstance(res,self.state_cls): state=res
            if node in self.cond:
                selector,mapping=self.cond[node]
                key=selector(state)
                if inspect.isawaitable(key): key=await key
                node=mapping[key]
            else:
                outs=[b for a,b in self.edges if a==node]
                node=outs[0] if outs else END
            steps+=1
        return dict(vars(state))
class StateGraph:
    def __init__(self, state_cls): self.state_cls=state_cls; self.nodes={}; self.edges=[]; self.cond={}; self.entry=None
    def add_node(self,name,fn): self.nodes[name]=fn
    def add_edge(self,a,b): self.edges.append((a,b))
    def add_conditional_edges(self,src,selector,mapping): self.cond[src]=(selector,mapping)
    def set_entry_point(self,name): self.entry=name
    def compile(self): return _Compiled(self.nodes,self.entry,self.edges,self.cond,self.state_cls)
